package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testConfig() config {
	return config{
		pollSeconds:    15,
		reportEvery:    60,
		warnPct:        80,
		warnSeconds:    60,
		topN:           20,
		historySamples: 240,
		diskPaths:      []string{"."},
		logChannelID:   -1001234567890,
		botToken:       "TEST",
		projectRoot:    "/tmp",
	}
}

func testSample(ts time.Time) Sample {
	gi := int64(1 << 30)
	return Sample{
		ts:        ts,
		cpuPct:    10.5,
		load1:     0.2,
		load5:     0.3,
		load15:    0.25,
		memTotal:  gi,
		memAvail:  int64(float64(gi) * 0.6),
		swapTotal: gi,
		swapUsed:  0,
		disks:     map[string]diskStat{".": {pct: 45.0, total: 2 * gi, free: int64(float64(2*gi) * 0.55)}},
		uptime:    86400 + 7200 + 300,
		cpuTop:    []procRow{{pid: 1, name: "python3", cpuPct: 5.0, rss: 50_000_000}},
		ramTop:    []procRow{{pid: 1, name: "python3", cpuPct: 5.0, rss: 50_000_000}},
	}
}

func TestFormatReport(t *testing.T) {
	ts := time.Date(2026, 8, 4, 5, 32, 11, 0, time.FixedZone("UTC", 0))
	s := testSample(ts)
	out := formatReport(testConfig(), []Sample{s, s, s}, s)

	checks := []string{
		"📊 <b>SYSTEM REPORT</b>",
		"<b>VPS time:</b> <code>2026-08-04 05:32:11</code>",
		"Window: last <b>1 min</b> (3 samples @ 15s) · Uptime 1d 2h 5m",
		"<b>CPU</b> now 10.5% · avg 10.5%",
		"<b>Load</b> 1m 0.20 / 5m 0.30 / 15m 0.25",
		"<b>RAM</b> now 40.0% · avg 40.0%  (410M / 1.0G)",
		"<b>Swap</b> now 0.0% · avg 0.0%  (0K / 1.0G)",
		"<b>disk</b> 45.0% used (1.1G free)",
		"🏆 <b>Top 1 by CPU</b> (this window)",
		`1. <code>python3</code> (pid 1) — CPU <b>5.0%</b> · RSS 48M`,
		"🧠 <b>Top 1 by RAM</b> (now)",
		`1. <code>python3</code> (pid 1) — RSS <b>48M</b> · CPU 5.0%`,
		"#system",
	}
	for _, c := range checks {
		if !strings.Contains(out, c) {
			t.Errorf("report missing %q\n---\n%s", c, out)
		}
	}
}

func TestFormatWarning(t *testing.T) {
	ts := time.Date(2026, 8, 4, 5, 33, 0, 0, time.FixedZone("UTC", 0))
	s := testSample(ts)
	s.cpuPct = 95.0
	s.disks = map[string]diskStat{".": {pct: 90.0, total: 2 << 30, free: 1 << 29}}

	cfg := testConfig()
	out := formatWarning(cfg, []Sample{s}, s)
	if out == "" {
		t.Fatal("expected a non-empty warning for hot sample")
	}
	checks := []string{
		"🚨 <b>HIGH SYSTEM USAGE</b>",
		"<b>VPS time:</b> <code>2026-08-04 05:33:00</code>",
		"Threshold: 80%. Currently: <b>CPU 95.0%, disk 90.0% (.)</b>",
		"Load: 0.20 / 0.30 / 0.25",
		"#system",
	}
	for _, c := range checks {
		if !strings.Contains(out, c) {
			t.Errorf("warning missing %q\n---\n%s", c, out)
		}
	}

	// A cool sample yields no warning.
	cool := testSample(ts)
	cool.cpuPct = 10.0
	cool.disks = map[string]diskStat{".": {pct: 30.0, total: 2 << 30, free: 1 << 30}}
	if got := formatWarning(cfg, []Sample{cool}, cool); got != "" {
		t.Errorf("expected empty warning for cool sample, got:\n%s", got)
	}
}

func TestFmtRSS(t *testing.T) {
	cases := []struct {
		in   int64
		want string
	}{
		{0, "0K"},
		{512, "0K"}, // 512B -> 0.5K rounds to 0 (banker's rounding, matches Python)
		{1 << 20, "1M"},
		{50_000_000, "48M"},
		{1 << 30, "1.0G"},
		{int64(1.5 * float64(1<<30)), "1.5G"},
	}
	for _, c := range cases {
		if got := fmtRSS(c.in); got != c.want {
			t.Errorf("fmtRSS(%d) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestUptimeStr(t *testing.T) {
	cases := []struct {
		in   float64
		want string
	}{
		{300, "5m"},
		{7200 + 300, "2h 5m"},
		{86400 + 7200 + 300, "1d 2h 5m"},
		{0, "0m"},
	}
	for _, c := range cases {
		if got := uptimeStr(c.in); got != c.want {
			t.Errorf("uptimeStr(%v) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestEsc(t *testing.T) {
	if got := esc(`<b>&"'`); got != "&lt;b&gt;&amp;&#34;&#39;" {
		t.Errorf("esc output mismatch: %q", got)
	}
}

func TestLoadDotenv(t *testing.T) {
	dir := t.TempDir()
	envPath := filepath.Join(dir, ".env")
	content := `
# comment
SYSMON_WARN_PCT=90
BOT_TOKEN=abc"quoted"
LOG_CHANNEL_ID=-100123
export SYSMON_TOP_N=5
`
	os.WriteFile(envPath, []byte(content), 0o644)

	// Clear in case the real environment has them.
	os.Unsetenv("SYSMON_WARN_PCT")
	os.Unsetenv("BOT_TOKEN")
	os.Unsetenv("LOG_CHANNEL_ID")
	os.Unsetenv("SYSMON_TOP_N")

	loadDotenv(envPath)

	if os.Getenv("SYSMON_WARN_PCT") != "90" {
		t.Errorf("SYSMON_WARN_PCT = %q, want 90", os.Getenv("SYSMON_WARN_PCT"))
	}
	if os.Getenv("BOT_TOKEN") != `abc"quoted"` {
		t.Errorf("BOT_TOKEN = %q, want abc\"quoted\"", os.Getenv("BOT_TOKEN"))
	}
	if os.Getenv("LOG_CHANNEL_ID") != "-100123" {
		t.Errorf("LOG_CHANNEL_ID = %q, want -100123", os.Getenv("LOG_CHANNEL_ID"))
	}
	if os.Getenv("SYSMON_TOP_N") != "5" {
		t.Errorf("SYSMON_TOP_N = %q, want 5", os.Getenv("SYSMON_TOP_N"))
	}

	// Real environment must win over .env.
	os.Setenv("SYSMON_WARN_PCT", "70")
	loadDotenv(envPath)
	if os.Getenv("SYSMON_WARN_PCT") != "70" {
		t.Errorf("real env should win: got %q", os.Getenv("SYSMON_WARN_PCT"))
	}
}

func TestFormatReportRich(t *testing.T) {
	ts := time.Date(2026, 8, 4, 5, 32, 11, 0, time.FixedZone("UTC", 0))
	s := testSample(ts)
	out := formatReportRich(testConfig(), []Sample{s, s, s}, s)

	checks := []string{
		"<h3>📊 SYSTEM REPORT</h3>",
		"<b>VPS time:</b> <code>2026-08-04 05:32:11</code>",
		"Window: last <b>1 min</b> (3 samples @ 15s) · Uptime 1d 2h 5m",
		"<table bordered><tr><th>Metric</th><th>Now</th><th>Avg</th></tr>",
		"<tr><td><b>CPU</b></td><td>10.5%</td><td>10.5%</td></tr>",
		"<tr><td><b>Load</b></td><td colspan=\"2\">1m 0.20 / 5m 0.30 / 15m 0.25</td></tr>",
		"<tr><td><b>RAM</b></td>",
		"<tr><td><b>Swap</b></td>",
		"<tr><td><b>disk</b></td><td>45.0% used</td><td>1.1G free</td></tr>",
		"<h4>🏆 Top 1 by CPU (this window)</h4>",
		"<table bordered>",
		"<tr><th align=\"right\">#</th><th>Process</th><th align=\"right\">PID</th><th align=\"right\">CPU</th><th align=\"right\">RSS</th></tr>",
		`<tr><td align="right">1</td><td><code>python3</code></td><td align="right">1</td><td align="right"><b>5.0%</b></td><td align="right">48M</td></tr>`,
		"<h4>🧠 Top 1 by RAM (now)</h4>",
		"<tr><th align=\"right\">#</th><th>Process</th><th align=\"right\">PID</th><th align=\"right\">RSS</th><th align=\"right\">CPU</th></tr>",
		`<tr><td align="right">1</td><td><code>python3</code></td><td align="right">1</td><td align="right"><b>48M</b></td><td align="right">5.0%</td></tr>`,
		"<footer>#system</footer>",
	}
	for _, c := range checks {
		if !strings.Contains(out, c) {
			t.Errorf("rich report missing %q\n---\n%s", c, out)
		}
	}
}

func TestFormatWarningRich(t *testing.T) {
	ts := time.Date(2026, 8, 4, 5, 33, 0, 0, time.FixedZone("UTC", 0))
	s := testSample(ts)
	s.cpuPct = 95.0
	s.disks = map[string]diskStat{".": {pct: 90.0, total: 2 << 30, free: 1 << 29}}

	cfg := testConfig()
	out := formatWarningRich(cfg, []Sample{s}, s)
	if out == "" {
		t.Fatal("expected a non-empty rich warning for hot sample")
	}
	checks := []string{
		"<h3>🚨 HIGH SYSTEM USAGE</h3>",
		"<b>VPS time:</b> <code>2026-08-04 05:33:00</code>",
		"<table bordered><tr><th>Metric</th><th>Value</th></tr>",
		"<tr><td>Threshold</td><td>80%</td></tr>",
		"<tr><td>Currently</td><td><b>CPU 95.0%, disk 90.0% (.)</b></td></tr>",
		"<tr><td>Load</td><td>0.20 / 0.30 / 0.25</td></tr>",
		"<footer>#system</footer>",
	}
	for _, c := range checks {
		if !strings.Contains(out, c) {
			t.Errorf("rich warning missing %q\n---\n%s", c, out)
		}
	}

	// A cool sample yields no rich warning either.
	cool := testSample(ts)
	cool.cpuPct = 10.0
	cool.disks = map[string]diskStat{".": {pct: 30.0, total: 2 << 30, free: 1 << 30}}
	if got := formatWarningRich(cfg, []Sample{cool}, cool); got != "" {
		t.Errorf("expected empty rich warning for cool sample, got:\n%s", got)
	}
}
