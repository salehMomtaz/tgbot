package main

import (
	"fmt"
	"html"
	"strings"
)

// Formatting helpers — byte-compatible with the Python system_monitor.py so the
// log channel shows exactly the same text. Do NOT change spacing/emoji/format
// specifiers without a reason; users read these messages.

func esc(s string) string {
	return html.EscapeString(s)
}

func fmtRSS(b int64) string {
	if b >= 1<<30 {
		return fmt.Sprintf("%.1fG", float64(b)/float64(1<<30))
	}
	if b >= 1<<20 {
		return fmt.Sprintf("%.0fM", float64(b)/float64(1<<20))
	}
	return fmt.Sprintf("%.0fK", float64(b)/float64(1<<10))
}

func avg(samples []Sample, getter func(Sample) float64) float64 {
	if len(samples) == 0 {
		return 0
	}
	var sum float64
	for _, s := range samples {
		sum += getter(s)
	}
	return sum / float64(len(samples))
}

func uptimeStr(seconds float64) string {
	secs := int(seconds)
	days := secs / 86400
	rem := secs % 86400
	hours := rem / 3600
	mins := (rem % 3600) / 60
	switch {
	case days > 0:
		return fmt.Sprintf("%dd %dh %dm", days, hours, mins)
	case hours > 0:
		return fmt.Sprintf("%dh %dm", hours, mins)
	default:
		return fmt.Sprintf("%dm", mins)
	}
}

func topRows(rows []procRow, kind string, topN int) []string {
	lines := make([]string, 0, len(rows))
	for i, r := range rows {
		if i >= topN {
			break
		}
		if kind == "cpu" {
			lines = append(lines, fmt.Sprintf("%d. <code>%s</code> (pid %d) — CPU <b>%.1f%%</b> · RSS %s",
				i+1, esc(r.name), r.pid, r.cpuPct, fmtRSS(r.rss)))
		} else {
			lines = append(lines, fmt.Sprintf("%d. <code>%s</code> (pid %d) — RSS <b>%s</b> · CPU %.1f%%",
				i+1, esc(r.name), r.pid, fmtRSS(r.rss), r.cpuPct))
		}
	}
	return lines
}

func formatReport(cfg config, samples []Sample, current Sample) string {
	windowMin := float64(len(samples)*cfg.pollSeconds) / 60.0
	avgCPU := avg(samples, func(s Sample) float64 { return s.cpuPct })
	avgMem := avg(samples, func(s Sample) float64 { return s.memPct() })
	avgSwap := avg(samples, func(s Sample) float64 { return s.swapPct() })

	nowStr := current.ts.Format("2006-01-02 15:04:05")

	lines := []string{
		"📊 <b>SYSTEM REPORT</b>",
		fmt.Sprintf("<b>VPS time:</b> <code>%s</code>", nowStr),
		fmt.Sprintf("Window: last <b>%.0f min</b> (%d samples @ %ds) · Uptime %s",
			windowMin, len(samples), cfg.pollSeconds, uptimeStr(current.uptime)),
		"",
		fmt.Sprintf("<b>CPU</b> now %.1f%% · avg %.1f%%", current.cpuPct, avgCPU),
		fmt.Sprintf("<b>Load</b> 1m %.2f / 5m %.2f / 15m %.2f", current.load1, current.load5, current.load15),
		fmt.Sprintf("<b>RAM</b> now %.1f%% · avg %.1f%%  (%s / %s)",
			current.memPct(), avgMem, fmtRSS(current.memTotal-current.memAvail), fmtRSS(current.memTotal)),
		fmt.Sprintf("<b>Swap</b> now %.1f%% · avg %.1f%%  (%s / %s)",
			current.swapPct(), avgSwap, fmtRSS(current.swapUsed), fmtRSS(current.swapTotal)),
	}

	for path, st := range current.disks {
		label := path
		if path == "." {
			label = "disk"
		}
		lines = append(lines, fmt.Sprintf("<b>%s</b> %.1f%% used (%s free)", esc(label), st.pct, fmtRSS(st.free)))
	}

	if len(current.cpuTop) > 0 {
		n := len(current.cpuTop)
		if n > cfg.topN {
			n = cfg.topN
		}
		lines = append(lines, "", fmt.Sprintf("🏆 <b>Top %d by CPU</b> (this window)", n))
		lines = append(lines, topRows(current.cpuTop, "cpu", cfg.topN)...)
	}
	if len(current.ramTop) > 0 {
		n := len(current.ramTop)
		if n > cfg.topN {
			n = cfg.topN
		}
		lines = append(lines, "", fmt.Sprintf("🧠 <b>Top %d by RAM</b> (now)", n))
		lines = append(lines, topRows(current.ramTop, "ram", cfg.topN)...)
	}

	lines = append(lines, "", "#system")
	return strings.Join(lines, "\n")
}

func formatWarning(cfg config, samples []Sample, current Sample) string {
	hot := []string{}
	if current.cpuPct >= float64(cfg.warnPct) {
		hot = append(hot, fmt.Sprintf("CPU %.1f%%", current.cpuPct))
	}
	if current.memPct() >= float64(cfg.warnPct) {
		hot = append(hot, fmt.Sprintf("RAM %.1f%%", current.memPct()))
	}
	for path, st := range current.disks {
		if st.pct >= float64(cfg.warnPct) {
			hot = append(hot, fmt.Sprintf("disk %.1f%% (%s)", st.pct, path))
		}
	}
	if len(hot) == 0 {
		return ""
	}

	nowStr := current.ts.Format("2006-01-02 15:04:05")

	lines := []string{
		"🚨 <b>HIGH SYSTEM USAGE</b>",
		fmt.Sprintf("<b>VPS time:</b> <code>%s</code>", nowStr),
		fmt.Sprintf("Threshold: %d%%. Currently: <b>%s</b>", cfg.warnPct, strings.Join(hot, ", ")),
		fmt.Sprintf("Load: %.2f / %.2f / %.2f", current.load1, current.load5, current.load15),
		"",
		fmt.Sprintf("⚠️ I will keep reporting every minute until everything drops below %d%%. Check for runaway downloads, a full disk, or a memory leak.", cfg.warnPct),
		"#system",
	}
	return strings.Join(lines, "\n")
}
