// Command tgbot-monitor is the standalone system monitor for tgbot.
//
// It is the Go port of the former utils/system_monitor.py: a /proc-only,
// zero-dependency health reporter that posts #system reports and 80% resource
// warnings to the bot's log channel via the plain Telegram Bot HTTP API — so
// it keeps working even when the bot process is down. It runs as its own
// process (the tgbot-monitor.service systemd unit, or a detached spawn from
// main.py), not inside the bot.
//
// Why Go (see docs/go-feasibility.md): the monitor is the one component whose
// profile — long-lived, resident, /proc-only, no shared library, must outlive
// the bot — matches Go's strengths. A static binary drops the resident RSS
// from ~27 MB (CPython) to a few MB on a 961 MB VPS, needs no interpreter or
// venv, and ships its own test suite.
//
// Design invariants (mirrored from the Python version, do not break):
//
//  1. ZERO third-party imports. Everything here is stdlib + /proc. Do not add
//     psutil/sigar/gopsutil.
//  2. INDEPENDENT of the bot. Raw net/http POST to the Bot API, never pyrogram.
//  3. Output format is part of the contract: the #system report and the warning
//     carry the VPS local date-time line, and the report/warning text must stay
//     byte-compatible with what the channel already shows (see format functions).
//  4. Never block the sampler: each send is fire-and-forget in a goroutine with
//     a short timeout; a bad sample is caught and skipped.
//  5. On start, write project-root/system_monitor.pid (own pid) and remove it
//     on exit, so utils/system_monitor.py::is_running() (pidfile + /proc scan)
//     can dedupe the systemd unit against the bot's detached spawn.
package main

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// ---------------------------------------------------------------------------
// Config (all overridable via .env — dotenv-style, NOT source'd)
// ---------------------------------------------------------------------------

type config struct {
	pollSeconds    int
	reportEvery    int
	warnPct        int
	warnSeconds    int
	topN           int
	historySamples int
	diskPaths      []string
	logChannelID   int64
	botToken       string
	requestsProxy  string
	projectRoot    string
	pidfile        string
}

func envInt(key string, def int) int {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil {
			return n
		}
	}
	return def
}

func envInt64(key string, def int64) int64 {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		var n int64
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil {
			return n
		}
	}
	return def
}

// loadDotenv reads a python-dotenv-style .env file and sets only the keys that
// are not already present in the real environment (load_dotenv semantics:
// real env wins). The systemd unit deliberately has no EnvironmentFile —
// run.sh owns .env parsing for the bot, and the monitor reads it itself so it
// stays standalone (invariant #6 in AGENTS.md).
func loadDotenv(path string) {
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		// Export-style lines are also fine: "export KEY=VALUE"
		if strings.HasPrefix(line, "export ") {
			line = strings.TrimSpace(strings.TrimPrefix(line, "export "))
		}
		eq := strings.IndexByte(line, '=')
		if eq <= 0 {
			continue
		}
		key := strings.TrimSpace(line[:eq])
		val := strings.TrimSpace(line[eq+1:])
		if key == "" {
			continue
		}
		// Strip surrounding single or double quotes.
		if len(val) >= 2 {
			if (val[0] == '"' && val[len(val)-1] == '"') || (val[0] == '\'' && val[len(val)-1] == '\'') {
				val = val[1 : len(val)-1]
			}
		}
		if os.Getenv(key) == "" {
			os.Setenv(key, val)
		}
	}
}

// resolveProjectRoot locates the repo root: the directory containing .env.
// We start at the current working directory (systemd sets WorkingDirectory to
// the project root; main.py's spawn sets the cwd too) and also try walking up
// from the executable, in case the binary is run from build/.
func resolveProjectRoot() string {
	for _, start := range []string{".", executableDir()} {
		abs, err := filepath.Abs(start)
		if err != nil {
			continue
		}
		dir := abs
		for depth := 0; depth < 4; depth++ {
			if _, err := os.Stat(filepath.Join(dir, ".env")); err == nil {
				return dir
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	// Fallback: cwd even if .env is absent (still functional for reporting).
	abs, _ := filepath.Abs(".")
	return abs
}

func executableDir() string {
	exe, err := os.Executable()
	if err != nil {
		return "."
	}
	return filepath.Dir(exe)
}

func loadConfig() config {
	// Project root first so dotenv can find .env regardless of cwd.
	root := resolveProjectRoot()
	loadDotenv(filepath.Join(root, ".env"))

	diskRaw := os.Getenv("SYSMON_DISK_PATHS")
	var diskPaths []string
	for _, p := range strings.Split(diskRaw, ",") {
		if p = strings.TrimSpace(p); p != "" {
			diskPaths = append(diskPaths, p)
		}
	}
	if len(diskPaths) == 0 {
		diskPaths = []string{"."}
	}

	return config{
		pollSeconds:    envInt("SYSMON_POLL_SECONDS", 15),
		reportEvery:    envInt("SYSMON_REPORT_INTERVAL", 60),
		warnPct:        envInt("SYSMON_WARN_PCT", 80),
		warnSeconds:    envInt("SYSMON_WARN_SECONDS", 60),
		topN:           envInt("SYSMON_TOP_N", 20),
		historySamples: envInt("SYSMON_HISTORY_SAMPLES", 240),
		diskPaths:      diskPaths,
		logChannelID:   envInt64("LOG_CHANNEL_ID", 0),
		botToken:       os.Getenv("BOT_TOKEN"),
		requestsProxy:  strings.TrimSpace(os.Getenv("REQUESTS_PROXY")),
		projectRoot:    root,
		pidfile:        filepath.Join(root, "system_monitor.pid"),
	}
}

// ---------------------------------------------------------------------------
// /proc readers (the same technique htop/top/ps use)
// ---------------------------------------------------------------------------

// readMeminfo returns {MemTotal, MemAvailable, SwapTotal, SwapFree} in bytes.
func readMeminfo() map[string]int64 {
	out := map[string]int64{"MemTotal": 0, "MemAvailable": 0, "SwapTotal": 0, "SwapFree": 0}
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return out
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		idx := strings.IndexByte(line, ':')
		if idx < 0 {
			continue
		}
		key := line[:idx]
		if _, ok := out[key]; !ok {
			continue
		}
		fields := strings.Fields(line[idx+1:])
		if len(fields) < 1 {
			continue
		}
		var kb int64
		fmt.Sscanf(fields[0], "%d", &kb)
		out[key] = kb * 1024 // kB -> bytes
	}
	return out
}

// readCPUTotal returns (idle_ticks, total_ticks) since boot.
func readCPUTotal() (int64, int64) {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return 0, 0
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	if !sc.Scan() {
		return 0, 0
	}
	parts := strings.Fields(sc.Text())
	// cpu  user nice system idle iowait irq softirq steal ...
	if len(parts) < 8 {
		return 0, 0
	}
	var nums [7]int64
	for i := 0; i < 7; i++ {
		fmt.Sscanf(parts[i+1], "%d", &nums[i])
	}
	// nums: user nice system idle iowait irq softirq
	idle := nums[3] + nums[4]
	total := nums[0] + nums[1] + nums[2] + nums[3] + nums[4] + nums[5] + nums[6]
	return idle, total
}

func readLoadavg() (float64, float64, float64) {
	data, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return 0, 0, 0
	}
	parts := strings.Fields(string(data))
	if len(parts) < 3 {
		return 0, 0, 0
	}
	var l1, l5, l15 float64
	fmt.Sscanf(parts[0], "%f", &l1)
	fmt.Sscanf(parts[1], "%f", &l5)
	fmt.Sscanf(parts[2], "%f", &l15)
	return l1, l5, l15
}

func readUptime() float64 {
	data, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	fields := strings.Fields(string(data))
	if len(fields) < 1 {
		return 0
	}
	var up float64
	fmt.Sscanf(fields[0], "%f", &up)
	return up
}

// clkTck is USER_HZ (100 on Linux). /proc/stat and /proc/<pid>/stat tick
// values are in clock ticks; the Python version used os.sysconf(SC_CLK_TCK).
var clkTck = int64(100)

// ---------------------------------------------------------------------------
// Process scanning (top-N by CPU and RAM) — needs tick deltas across polls
// ---------------------------------------------------------------------------

type procSample struct {
	name  string
	ticks int64
	rss   int64
}

var procPrev = map[int]procSample{} // pid -> previous poll snapshot
var lastPollWall = time.Now()

func scanProcesses(cfg config) (cpuTop, ramTop []procRow) {
	now := time.Now()
	deltaWall := now.Sub(lastPollWall).Seconds()
	if deltaWall < 0.1 {
		deltaWall = 0.1
	}
	lastPollWall = now

	cur := map[int]procSample{}
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil, nil
	}
	for _, e := range entries {
		if !isAllDigits(e.Name()) {
			continue
		}
		pid := atoiSafe(e.Name())
		if pid <= 0 {
			continue
		}
		name, ticks, rss := readProcStats(pid)
		if ticks == 0 && rss == 0 && name == "" {
			continue
		}
		cur[pid] = procSample{name: name, ticks: ticks, rss: rss}
	}

	rows := make([]procRow, 0, len(cur))
	for pid, s := range cur {
		cpuPct := 0.0
		if prev, ok := procPrev[pid]; ok {
			dticks := s.ticks - prev.ticks
			if dticks < 0 {
				dticks = 0
			}
			cpuPct = (float64(dticks) / float64(clkTck)) / deltaWall * 100.0
		}
		rows = append(rows, procRow{pid: pid, name: s.name, cpuPct: cpuPct, rss: s.rss})
	}
	procPrev = map[int]procSample{}
	for pid, s := range cur {
		procPrev[pid] = s
	}

	cpuTop = topBy(rows, func(r procRow) float64 { return -r.cpuPct }, cfg.topN)
	ramTop = topBy(rows, func(r procRow) float64 { return -float64(r.rss) }, cfg.topN)
	return cpuTop, ramTop
}

// readProcStats parses /proc/<pid>/stat (comm may contain spaces/parens, so
// split on the last ')') and /proc/<pid>/status for VmRSS.
func readProcStats(pid int) (name string, ticks, rss int64) {
	statPath := fmt.Sprintf("/proc/%d/stat", pid)
	data, err := os.ReadFile(statPath)
	if err != nil {
		return "", 0, 0
	}
	s := string(data)
	lp := strings.IndexByte(s, '(')
	rp := strings.LastIndexByte(s, ')')
	if lp < 0 || rp < 0 || rp < lp {
		return "", 0, 0
	}
	comm := s[lp+1 : rp]
	if len(comm) > 40 {
		comm = comm[:40]
	}
	rest := strings.Fields(s[rp+2:])
	if len(rest) < 14 { // need at least indices 11,12
		return "", 0, 0
	}
	utime := atoi64Safe(rest[11])
	stime := atoi64Safe(rest[12])

	// VmRSS from /proc/<pid>/status
	statusPath := fmt.Sprintf("/proc/%d/status", pid)
	if sf, err := os.Open(statusPath); err == nil {
		sc := bufio.NewScanner(sf)
		for sc.Scan() {
			line := sc.Text()
			if strings.HasPrefix(line, "VmRSS:") {
				fields := strings.Fields(line)
				if len(fields) >= 2 {
					var kb int64
					fmt.Sscanf(fields[1], "%d", &kb)
					rss = kb * 1024
				}
				break
			}
		}
		sf.Close()
	}
	return comm, utime + stime, rss
}

type procRow struct {
	pid    int
	name   string
	cpuPct float64
	rss    int64
}

func topBy(rows []procRow, less func(procRow) float64, n int) []procRow {
	// simple insertion sort — the list is /proc-sized, plenty fast
	for i := 1; i < len(rows); i++ {
		key := rows[i]
		j := i - 1
		for j >= 0 && less(rows[j]) > less(key) {
			rows[j+1] = rows[j]
			j--
		}
		rows[j+1] = key
	}
	if len(rows) > n {
		rows = rows[:n]
	}
	return rows
}

func isAllDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

func atoiSafe(s string) int {
	var n int
	if _, err := fmt.Sscanf(s, "%d", &n); err == nil {
		return n
	}
	return 0
}

func atoi64Safe(s string) int64 {
	var n int64
	if _, err := fmt.Sscanf(s, "%d", &n); err == nil {
		return n
	}
	return 0
}

// ---------------------------------------------------------------------------
// Sample model
// ---------------------------------------------------------------------------

type Sample struct {
	ts        time.Time
	cpuPct    float64
	load1     float64
	load5     float64
	load15    float64
	memTotal  int64
	memAvail  int64
	swapTotal int64
	swapUsed  int64
	disks     map[string]diskStat // path -> stat
	uptime    float64
	cpuTop    []procRow
	ramTop    []procRow
}

type diskStat struct {
	pct   float64
	total int64
	free  int64
}

func (s *Sample) memPct() float64 {
	if s.memTotal <= 0 {
		return 0
	}
	return maxf(100.0*float64(s.memTotal-s.memAvail)/float64(s.memTotal), 0.0)
}

func (s *Sample) swapPct() float64 {
	if s.swapTotal <= 0 {
		return 0
	}
	return maxf(100.0*float64(s.swapUsed)/float64(s.swapTotal), 0.0)
}

func maxf(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}

func collect(cfg config) (Sample, error) {
	idle1, total1 := readCPUTotal()
	time.Sleep(1 * time.Second)
	idle2, total2 := readCPUTotal()
	dTotal := total2 - total1
	cpuPct := 0.0
	if dTotal > 0 {
		cpuPct = 100.0 * float64(dTotal-(idle2-idle1)) / float64(dTotal)
	}

	mem := readMeminfo()
	l1, l5, l15 := readLoadavg()
	up := readUptime()

	disks := map[string]diskStat{}
	for _, p := range cfg.diskPaths {
		if st, err := diskUsage(p); err == nil {
			disks[p] = st
		}
	}

	cpuTop, ramTop := scanProcesses(cfg)
	return Sample{
		ts:        time.Now(),
		cpuPct:    cpuPct,
		load1:     l1,
		load5:     l5,
		load15:    l15,
		memTotal:  mem["MemTotal"],
		memAvail:  mem["MemAvailable"],
		swapTotal: mem["SwapTotal"],
		swapUsed:  max64(mem["SwapTotal"]-mem["SwapFree"], 0),
		disks:     disks,
		uptime:    up,
		cpuTop:    cpuTop,
		ramTop:    ramTop,
	}, nil
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

// diskUsage mirrors shutil.disk_usage via statfs.
func diskUsage(path string) (diskStat, error) {
	var fs syscall.Statfs_t
	if err := syscall.Statfs(path, &fs); err != nil {
		return diskStat{}, err
	}
	total := int64(fs.Blocks) * int64(fs.Bsize)
	free := int64(fs.Bavail) * int64(fs.Bsize)
	pct := 0.0
	if total > 0 {
		pct = 100.0 * float64(total-free) / float64(total)
	}
	return diskStat{pct: pct, total: total, free: free}, nil
}
