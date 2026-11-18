#!/bin/sh
# Minimal ldd-compatible wrapper for Synology DSM systems that ship glibc
# and its dynamic loader but omit the ldd command.
set -u

LOADER=""
for candidate in \
    /lib/ld-linux-x86-64.so.2 \
    /lib64/ld-linux-x86-64.so.2 \
    /usr/lib/ld-linux-x86-64.so.2
do
    if [ -x "$candidate" ]; then
        LOADER=$candidate
        break
    fi
done

if [ -z "$LOADER" ]; then
    printf '%s\n' "ldd compatibility: no x86_64 glibc dynamic loader was found." >&2
    exit 1
fi

if [ "$#" -eq 1 ]; then
    case "$1" in
        --version|-v)
            printf '%s\n' "ldd (CtrlVee Synology dynamic-loader compatibility)"
            exit 0
            ;;
        --help|-h)
            printf '%s\n' "Usage: ldd FILE"
            exit 0
            ;;
    esac
fi

if [ "$#" -ne 1 ]; then
    printf '%s\n' "Usage: ldd FILE" >&2
    exit 1
fi

output=$("$LOADER" --list "$1" 2>&1)
status=$?
printf '%s\n' "$output"

if [ "$status" -ne 0 ]; then
    # The glibc loader reports a missing dependency as an error rather than
    # emitting ldd's usual "NAME => not found" line. Normalize that case so
    # GitHub runner config.sh's existing grep check detects it.
    case "$output" in
        *"error while loading shared libraries:"*)
            missing=${output#*error while loading shared libraries: }
            missing=${missing%%:*}
            printf '%s => not found\n' "$missing"
            exit 0
            ;;
    esac
    exit "$status"
fi

exit 0
