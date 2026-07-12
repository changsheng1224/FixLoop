FROM openjdk:17-slim
RUN apt-get update && apt-get install -y git
COPY sandbox/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
