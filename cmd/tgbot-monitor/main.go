package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"
)

// ---------------------------------------------------------------------------
// Telegram sending — raw Bot HTTP API (independent of pyrogram, like the old
// Python logger). Fire-and-forget with a short timeout in a goroutine so the
// sampler never blocks.
// ---------------------------------------------------------------------------

func sendTelegram(cfg config, richText, plainText string) {
	if cfg.botToken == "" || cfg.logChannelID == 0 {
		return
	}
	richPayload := map[string]interface{}{
		"chat_id":      cfg.logChannelID,
		"rich_message": map[string]interface{}{"html": richText[:minInt(len(richText), 32000)]},
	}
	fallbackPayload := map[string]interface{}{
		"chat_id":    cfg.logChannelID,
		"text":       plainText[:minInt(len(plainText), 4000)],
		"parse_mode": "HTML",
	}

	// Try the rich endpoint first; the plain sendMessage is the fallback for
	// Bot API versions that don't support sendRichMessage yet.
	go func() {
		richOK := postJSON(cfg, "sendRichMessage", richPayload)
		if !richOK {
			postJSON(cfg, "sendMessage", fallbackPayload)
		}
	}()
}

// postJSON POSTs payload to method on the Bot HTTP API and reports whether the
// call succeeded. A non-2xx status or a JSON body with ok:false counts as
// failure (drives the rich→plain fallback).
func postJSON(cfg config, method string, payload map[string]interface{}) bool {
	body, err := json.Marshal(payload)
	if err != nil {
		return false
	}
	urlStr := fmt.Sprintf("https://api.telegram.org/bot%s/%s", cfg.botToken, method)

	req, err := http.NewRequest("POST", urlStr, bytes.NewReader(body))
	if err != nil {
		return false
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 8 * time.Second}
	if cfg.requestsProxy != "" {
		if u, err := url.Parse(cfg.requestsProxy); err == nil {
			client.Transport = &http.Transport{Proxy: http.ProxyURL(u)}
		}
	}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false
	}
	var out struct {
		OK bool `json:"ok"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return false
	}
	return out.OK
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// ---------------------------------------------------------------------------
// Pidfile — the bot's utils/system_monitor.py::is_running() checks this file
// (and a /proc cmdline scan) to dedupe the systemd unit against a detached
// spawn. We write our own pid on start and remove it on exit.
// ---------------------------------------------------------------------------

func writePidfile(cfg config) {
	os.WriteFile(cfg.pidfile, []byte(fmt.Sprintf("%d\n", os.Getpid())), 0o644)
}

func removePidfile(cfg config) {
	os.Remove(cfg.pidfile)
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

func run(cfg config) int {
	if cfg.botToken == "" || cfg.logChannelID == 0 {
		fmt.Fprintln(os.Stderr, "[system_monitor] ERROR: BOT_TOKEN and LOG_CHANNEL_ID must be set in .env.")
		return 2
	}

	writePidfile(cfg)
	defer removePidfile(cfg)

	// Signal handling: SIGTERM (systemd stop) and SIGINT both exit cleanly.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	done := make(chan struct{})
	go func() {
		<-sigCh
		close(done)
	}()

	fmt.Printf("[system_monitor] started. poll=%ds, report every %ds, warn >=%d%% every %ds.\n",
		cfg.pollSeconds, cfg.reportEvery*cfg.pollSeconds, cfg.warnPct, cfg.warnSeconds)
	fmt.Printf("[system_monitor] channel=%d paths=%v\n", cfg.logChannelID, cfg.diskPaths)

	samples := []Sample{}
	reportCount := 0
	lastWarnTs := time.Unix(0, 0)
	warnActive := false

	for {
		select {
		case <-done:
			fmt.Println("[system_monitor] stopped.")
			return 0
		default:
		}

		s, err := collect(cfg)
		if err != nil {
			fmt.Fprintf(os.Stderr, "[system_monitor] sample error: %v\n", err)
			time.Sleep(time.Duration(cfg.pollSeconds) * time.Second)
			continue
		}

		reportCount++
		samples = append(samples, s)
		if len(samples) > cfg.historySamples {
			samples = samples[len(samples)-cfg.historySamples:]
		}

		// --- Threshold warning: fire when hot, repeat every WARN_SECONDS. ---
		isHot := s.cpuPct >= float64(cfg.warnPct) ||
			s.memPct() >= float64(cfg.warnPct) ||
			anyDiskHot(cfg, s)
		if isHot {
			if !warnActive || time.Since(lastWarnTs) >= time.Duration(cfg.warnSeconds)*time.Second {
				text := formatWarning(cfg, samples, s)
				if text != "" {
					sendTelegram(cfg, formatWarningRich(cfg, samples, s), text)
					fmt.Printf("[system_monitor] warning sent (%s)\n", secondLine(text))
				}
				lastWarnTs = time.Now()
			}
			warnActive = true
		} else {
			warnActive = false
		}

		// --- Periodic report every REPORT_EVERY samples. ---
		// Counts on a MONOTONIC reportCount, never on len(samples): the
		// history buffer is trimmed to historySamples, so its length pins at
		// that cap and `len(samples) % reportEvery == 0` would stop firing
		// after the first 240 samples — the monitor then goes silent forever.
		if shouldReportReport(reportCount, cfg.reportEvery) {
			sendTelegram(cfg, formatReportRich(cfg, samples, s), formatReport(cfg, samples, s))
			fmt.Printf("[system_monitor] report sent (%d samples)\n", reportCount)
		}

		time.Sleep(time.Duration(cfg.pollSeconds) * time.Second)
	}
}

func anyDiskHot(cfg config, s Sample) bool {
	for _, st := range s.disks {
		if st.pct >= float64(cfg.warnPct) {
			return true
		}
	}
	return false
}

// shouldReportReport returns true every reportEvery-th sample, counting on a
// monotonic counter so the report cadence survives the history buffer being
// trimmed to historySamples (regression: len(samples)%reportEvery used to pin
// at the cap and go silent forever after the first historySamples samples).
func shouldReportReport(reportCount, reportEvery int) bool {
	return reportEvery > 0 && reportCount > 0 && reportCount%reportEvery == 0
}

func secondLine(text string) string {
	lines := splitLines(text)
	if len(lines) >= 2 {
		return lines[1]
	}
	return lines[0]
}

func splitLines(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			out = append(out, s[start:i])
			start = i + 1
		}
	}
	out = append(out, s[start:])
	return out
}

func main() {
	if len(os.Args) > 1 && (os.Args[1] == "--version" || os.Args[1] == "-v") {
		fmt.Println("tgbot-monitor 1.1.0 (Go)")
		return
	}
	cfg := loadConfig()
	os.Exit(run(cfg))
}
