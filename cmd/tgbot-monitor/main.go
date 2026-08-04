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

func sendTelegram(cfg config, text string) {
	if cfg.botToken == "" || cfg.logChannelID == 0 {
		return
	}
	payload := map[string]interface{}{
		"chat_id":    cfg.logChannelID,
		"text":       text[:minInt(len(text), 4000)],
		"parse_mode": "HTML",
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return
	}
	urlStr := fmt.Sprintf("https://api.telegram.org/bot%s/sendMessage", cfg.botToken)

	go func() {
		req, err := http.NewRequest("POST", urlStr, bytes.NewReader(body))
		if err != nil {
			return
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
			return
		}
		resp.Body.Close()
	}()
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
	lastReportIdx := -1
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
					sendTelegram(cfg, text)
					fmt.Printf("[system_monitor] warning sent (%s)\n", secondLine(text))
				}
				lastWarnTs = time.Now()
			}
			warnActive = true
		} else {
			warnActive = false
		}

		// --- Periodic report every REPORT_EVERY_SAMPLES samples. ---
		if len(samples)%cfg.reportEvery == 0 && (len(samples)-1) != lastReportIdx {
			sendTelegram(cfg, formatReport(cfg, samples, s))
			lastReportIdx = len(samples) - 1
			fmt.Printf("[system_monitor] report sent (%d samples)\n", len(samples))
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
		fmt.Println("tgbot-monitor 1.0.0 (Go)")
		return
	}
	cfg := loadConfig()
	os.Exit(run(cfg))
}
