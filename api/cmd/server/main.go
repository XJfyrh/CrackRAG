package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"crackrag/api/internal/app"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))
	if len(os.Args) > 1 && os.Args[1] == "health" {
		client := http.Client{Timeout: 3 * time.Second}
		address := os.Getenv("M1_HTTP_ADDRESS")
		if address == "" {
			address = "127.0.0.1:8080"
		}
		resp, err := client.Get("http://" + address + "/healthz")
		if err != nil || resp.StatusCode != 200 {
			os.Exit(1)
		}
		resp.Body.Close()
		return
	}
	cfg, err := app.LoadConfig()
	if err != nil {
		slog.Error("configuration rejected", "reason", err.Error())
		os.Exit(1)
	}
	if len(os.Args) > 1 && os.Args[1] == "check-config" {
		slog.Info("configuration valid", "protocol", app.ProtocolVersion, "provider", cfg.Provider)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	server, err := app.New(ctx, cfg)
	cancel()
	if err != nil {
		slog.Error("startup failed", "reason", "DATABASE_OR_MIGRATION_UNAVAILABLE")
		os.Exit(1)
	}
	defer server.Close()
	grpcServer, err := server.GRPCServer()
	if err != nil {
		slog.Error("startup failed", "reason", "GRPC_LISTEN_FAILED")
		os.Exit(1)
	}
	defer grpcServer.Stop()
	httpServer := &http.Server{Addr: cfg.HTTPAddress, Handler: server.Router(), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
	go func() {
		slog.Info("api ready", "address", cfg.HTTPAddress, "protocol", app.ProtocolVersion, "provider", cfg.Provider)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("http stopped", "reason", "HTTP_SERVER_STOPPED")
		}
	}()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	<-signals
	shutdown, stop := context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	httpServer.Shutdown(shutdown)
}
