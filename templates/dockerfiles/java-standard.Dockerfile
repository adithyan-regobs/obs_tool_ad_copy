FROM eclipse-temurin:{{JDK_VERSION}}-jdk

ARG JAR_FILE
ARG PROFILE
{{CUSTOM_BUILD_ARGS}}

ENV profile=$PROFILE
ENV APP_HOME=/usr/app/

WORKDIR $APP_HOME

COPY ${JAR_FILE} /app.jar
RUN chmod 755 /app.jar

{{DATADOG_BLOCK}}
ENTRYPOINT ["sh", "-c", "java -jar /app.jar --spring.profiles.active=${profile}"]
