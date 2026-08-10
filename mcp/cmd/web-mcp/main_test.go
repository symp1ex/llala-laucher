package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func TestStdioHelperProcess(t *testing.T) {
	if os.Getenv("WEB_MCP_HELPER") != "1" {
		return
	}
	os.Args = []string{"web-mcp", "--searxng-url", os.Getenv("WEB_MCP_SEARXNG"), "--timeout", "1"}
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Exit(0)
}

func TestRawStdioProtocolIDsErrorsEOFAndCleanStdout(t *testing.T) {
	searxng := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("q") == "force HTTP error" {
			http.Error(w, "temporary", http.StatusBadGateway)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"results":[{"title":"Mock result","url":"https://example.com","content":"snippet","engines":["mock"]}]}`)
	}))
	defer searxng.Close()
	cmd := exec.Command(os.Args[0], "-test.run=TestStdioHelperProcess")
	cmd.Env = append(os.Environ(), "WEB_MCP_HELPER=1", "WEB_MCP_SEARXNG="+searxng.URL)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	lines := make(chan string, 16)
	go func() {
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			lines <- scanner.Text()
		}
		close(lines)
	}()
	write := func(line string) {
		t.Helper()
		if _, err := io.WriteString(stdin, line+"\n"); err != nil {
			t.Fatal(err)
		}
	}
	read := func() map[string]any {
		t.Helper()
		select {
		case line, ok := <-lines:
			if !ok {
				t.Fatalf("stdout closed early; stderr=%s", stderr.String())
			}
			var response map[string]any
			if err := json.Unmarshal([]byte(line), &response); err != nil {
				t.Fatalf("non-JSON text in MCP stdout: %q", line)
			}
			return response
		case <-time.After(5 * time.Second):
			t.Fatalf("timed out waiting for MCP response; stderr=%s", stderr.String())
			return nil
		}
	}

	write(`{"jsonrpc":"2.0","id":"init-id","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"raw-test","version":"1"}}}`)
	initialize := read()
	if initialize["id"] != "init-id" || initialize["result"] == nil {
		t.Fatalf("initialize id/result mismatch: %v", initialize)
	}
	write(`{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}`)
	write(`{"jsonrpc":"2.0","id":27,"method":"tools/list","params":{}}`)
	tools := read()
	if tools["id"] != float64(27) || !strings.Contains(fmt.Sprint(tools["result"]), "web_search") || !strings.Contains(fmt.Sprint(tools["result"]), "web_fetch") {
		t.Fatalf("unexpected tools/list response: %v", tools)
	}
	write(`{"jsonrpc":"2.0","id":"search-call","method":"tools/call","params":{"name":"web_search","arguments":{"query":"mock query"}}}`)
	searchCall := read()
	if searchCall["id"] != "search-call" || !strings.Contains(fmt.Sprint(searchCall["result"]), "Mock result") {
		t.Fatalf("unexpected web_search response: %v", searchCall)
	}
	write(`{"jsonrpc":"2.0","id":"search-error","method":"tools/call","params":{"name":"web_search","arguments":{"query":"force HTTP error"}}}`)
	searchError := read()
	if searchError["id"] != "search-error" || !strings.Contains(fmt.Sprint(searchError["result"]), "isError:true") || !strings.Contains(fmt.Sprint(searchError["result"]), "HTTP 502") {
		t.Fatalf("unexpected web_search HTTP error response: %v", searchError)
	}
	write(`{"jsonrpc":"2.0","id":"fetch-error","method":"tools/call","params":{"name":"web_fetch","arguments":{"url":"http://127.0.0.1/private"}}}`)
	fetchError := read()
	if fetchError["id"] != "fetch-error" || !strings.Contains(fmt.Sprint(fetchError["result"]), "isError:true") || !strings.Contains(fmt.Sprint(fetchError["result"]), "SSRF") {
		t.Fatalf("unexpected web_fetch SSRF response: %v", fetchError)
	}
	write(`{"jsonrpc":"2.0","id":"unknown-id","method":"unknown/method","params":{}}`)
	unknown := read()
	if unknown["id"] != "unknown-id" || unknown["error"] == nil {
		t.Fatalf("unknown method did not return JSON-RPC error: %v", unknown)
	}
	write(`{"jsonrpc":"2.0","id":"invalid-rpc","method":"tools/call","params":{"name":1,"arguments":{}}}`)
	invalid := read()
	if invalid["error"] == nil {
		t.Fatalf("invalid JSON-RPC did not return an error: %v", invalid)
	}
	write(`{"jsonrpc":"2.0","id":"ping-id","method":"ping","params":{}}`)
	ping := read()
	if ping["id"] != "ping-id" || ping["result"] == nil {
		t.Fatalf("ping failed after malformed JSON: %v", ping)
	}
	if err := stdin.Close(); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("helper failed on EOF: %v; stderr=%s", err, stderr.String())
		}
	case <-time.After(5 * time.Second):
		_ = cmd.Process.Kill()
		t.Fatal("MCP process did not exit on EOF")
	}
	if stderr.Len() != 0 {
		t.Fatalf("ordinary tool network error leaked diagnostics to stderr: %s", stderr.String())
	}
}
