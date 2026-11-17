#!/bin/sh
set -eu

: "${VLC_TEST_PASSWORD:?VLC_TEST_PASSWORD must be set}"

exec cvlc \
    --intf dummy \
    --extraintf http \
    --http-host 0.0.0.0 \
    --http-port 8080 \
    --http-password "$VLC_TEST_PASSWORD" \
    --no-video \
    --aout dummy \
    --no-one-instance \
    --no-media-library \
    --ignore-config
