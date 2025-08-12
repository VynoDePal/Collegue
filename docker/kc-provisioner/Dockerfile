FROM alpine:3.20
RUN apk add --no-cache curl jq
COPY scripts/kc-provision.sh /usr/local/bin/kc-provision.sh
RUN chmod +x /usr/local/bin/kc-provision.sh
ENTRYPOINT ["/bin/sh", "-lc", "/bin/sh /usr/local/bin/kc-provision.sh"]
