#!/bin/sh
# Keep a Kerberos ticket for $KRB_PRINCIPAL in /krb/ccache.
#
# This runs in its own container of the backend pod. The password, krb5.conf and ticket cache are
# mounted only here, so the backend (and the dataset agent inside it) can never read them.
#
# A failed kinit is logged and retried, never fatal: a missing Secret or an unreachable KDC must
# not take the pod down.

set -u

: "${KRB_PRINCIPAL:?KRB_PRINCIPAL is not set}"
PASSWORD_FILE="${KRB_PASSWORD_FILE:-/etc/kerberos/password}"
CCACHE="${KRB_CCACHE:-/krb/ccache}"
REFRESH_SECONDS="${KRB_REFRESH_SECONDS:-21600}"
RETRY_SECONDS="${KRB_RETRY_SECONDS:-300}"

refresh() {
    if [ ! -r "$PASSWORD_FILE" ]; then
        echo "kinit failed: no password at $PASSWORD_FILE" >&2
        return 1
    fi
    # The password goes in on stdin. Write a new cache and rename it over the old one, so a
    # reader never sees a half-written cache and a failed kinit leaves the old ticket in place.
    if out=$(KRB5CCNAME="FILE:$CCACHE.new" kinit "$KRB_PRINCIPAL" <"$PASSWORD_FILE" 2>&1); then
        mv "$CCACHE.new" "$CCACHE"
        echo "kinit: new ticket for $KRB_PRINCIPAL"
    else
        rm -f "$CCACHE.new"
        # kinit's output is its prompt and error message, never the password
        echo "kinit failed: $(printf %s "$out" | sed 's/^Password for [^:]*: *//')" >&2
        return 1
    fi
}

# Sleep in the background so SIGTERM stops the container straight away
trap 'exit 0' TERM INT
while :; do
    if refresh; then wait_for="$REFRESH_SECONDS"; else wait_for="$RETRY_SECONDS"; fi
    sleep "$wait_for" &
    wait $!
done
