FROM maven:3.9.11-eclipse-temurin-21@sha256:6fdc855a6ed81d288ca7ca37ac6ff5e9308b612485c0801d70b25a858c83d237 AS build
WORKDIR /workspace
COPY implementations/java/pom.xml ./pom.xml
COPY implementations/java/.mvn/settings.xml /tmp/maven-settings.xml
RUN mvn -s /tmp/maven-settings.xml -B dependency:go-offline
COPY implementations/java/src ./src
COPY contracts ./contracts
RUN mvn -s /tmp/maven-settings.xml -B package
FROM eclipse-temurin:21-jre@sha256:d77e090ae1f6970c047b443c6f0a6c1358b0426d44432bb5a7753408cd43de0d
WORKDIR /app
COPY --from=build /workspace/target/psp-reference-java-2.0.0.jar /app/gateway.jar
COPY contracts /app/contracts
COPY storage /app/storage
ENV CONTRACT_FILE=/app/contracts/runtime.json MIGRATION_FILE=/app/storage/migrations/001_init.sql
USER 10001
EXPOSE 8080
ENTRYPOINT ["java","-jar","/app/gateway.jar"]
