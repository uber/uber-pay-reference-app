package com.uber.pay.reference;

import java.util.Map;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;

@SpringBootApplication
public class Application {
  @Bean
  org.springframework.boot.web.servlet.FilterRegistrationBean<BoundedBodyFilter> boundedBody() {
    var filter =
        new org.springframework.boot.web.servlet.FilterRegistrationBean<>(new BoundedBodyFilter());
    filter.setAsyncSupported(true);
    filter.setDispatcherTypes(jakarta.servlet.DispatcherType.REQUEST);
    filter.setOrder(Integer.MIN_VALUE);
    return filter;
  }

  public static void main(String[] args) {
    Config c = new Config(System.getenv());
    SpringApplication app = new SpringApplication(Application.class);
    app.setDefaultProperties(
        Map.of(
            "server.address",
            c.host,
            "server.port",
            c.port,
            "server.max-http-request-header-size",
            "16KB",
            "spring.main.banner-mode",
            "off",
            "server.error.include-message",
            "never",
            "server.error.include-stacktrace",
            "never",
            "logging.level.root",
            "WARN"));
    app.addInitializers(context -> context.getBeanFactory().registerSingleton("gatewayConfig", c));
    app.run(args);
  }

  @Bean
  Database database(Config c) {
    Database d = new Database(c);
    d.initialize();
    return d;
  }

  @Bean
  Contracts contracts(Config c) {
    Contracts ct = new Contracts(c.contract);
    if (c.strictAccountName)
      ((com.fasterxml.jackson.databind.node.ArrayNode)
              ct.spec.path("components").path("schemas").path("Account").path("required"))
          .add("name");
    if (c.strictDescription) {
      var desc =
          (com.fasterxml.jackson.databind.node.ObjectNode)
              ct.spec.path("components").path("schemas").path("TransactionDescription");
      desc.set("pattern", desc.get("x-upstream-pattern"));
    }
    return ct;
  }

  @Bean
  @org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean(PspAdapter.class)
  PspAdapter pspAdapter() {
    return new DemoPspAdapter();
  }

  @Bean
  PaymentService payments(Config c, Database d, Contracts ct, PspAdapter a) {
    return new PaymentService(c, d, ct, a);
  }

  @Bean
  @org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean(WalletProvider.class)
  WalletProvider walletProvider(Config c) {
    return new DemoWalletProvider(c.fingerprintKey);
  }

  @Bean
  WalletService wallets(Config c, Database d, Contracts ct, WalletProvider provider) {
    return new WalletService(c, d, ct, provider);
  }

  @Bean(initMethod = "start", destroyMethod = "close")
  ProcessorExecutor processor(PaymentService p) {
    return new ProcessorExecutor(p);
  }

  @Bean(initMethod = "start", destroyMethod = "close")
  CallbackWorker worker(Config c, Database d, PaymentService p, Contracts ct) {
    return new CallbackWorker(c, d, p, ct);
  }
}
