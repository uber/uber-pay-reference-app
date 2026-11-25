package main

import (
	"context"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"uberpay-reference/go/internal/gateway"
)

func main() {
	c, e := gateway.LoadConfig()
	if e != nil {
		log.Fatalf("configuration rejected: %s", e)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	app, e := gateway.New(ctx, c)
	if e != nil {
		log.Fatalf("startup failed: %s", e)
	}
	defer app.Close()
	go app.RunWorkers(ctx)
	if c.ProcessorMode == "http" {
		go app.RunProcessor(ctx)
	}
	server := &http.Server{Addr: net.JoinHostPort(c.Host, c.Port), Handler: app, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16384}
	go func() {
		<-ctx.Done()
		shutdown, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		_ = server.Shutdown(shutdown)
	}()
	log.Printf("local synthetic Go PSP on %s", server.Addr)
	if e = server.ListenAndServe(); e != nil && e != http.ErrServerClosed {
		log.Fatal(e)
	}
}
