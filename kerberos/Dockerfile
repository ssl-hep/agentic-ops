FROM debian:trixie-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends krb5-user \
    && rm -rf /var/lib/apt/lists/*

COPY kinit-loop.sh /usr/local/bin/kinit-loop

# Not the backend's uid 10001
RUN useradd --no-create-home --uid 10002 kerberos
USER 10002

ENV KRB5CCNAME=FILE:/krb/ccache
CMD ["/usr/local/bin/kinit-loop"]
