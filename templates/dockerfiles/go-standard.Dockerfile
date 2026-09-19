FROM debian:bullseye-slim

{{AWS_SECRETS_BLOCK}}
{{CUSTOM_BUILD_ARGS}}
ENV CONFIG_PATH="{{CONFIG_PATH}}"
ENV CONFIG_TYPE="{{CONFIG_TYPE}}"

RUN apt-get update && apt-get install -y ca-certificates && apt-get clean

WORKDIR /app
COPY app .
{{CONFIG_COPY_LINE}}

ENV APP_PORT={{PORT}}
EXPOSE {{PORT}}

CMD ["./app"]
