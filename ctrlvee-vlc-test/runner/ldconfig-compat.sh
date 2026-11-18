#!/bin/sh
# Read-only compatibility shim for the narrow ldconfig probe in GitHub
# Actions runner config.sh. DSM may have a working glibc loader/cache but no
# ldconfig executable. The upstream probe only needs to discover libicu.
set -u

# config.sh invokes: ldconfig -NXv ${LD_LIBRARY_PATH//:/ }
# -N and -X explicitly disable cache/link updates; this shim never writes.
directories="/lib /lib64 /usr/lib /usr/lib64 /usr/local/lib /usr/local/lib64"

for argument do
    case "$argument" in
        -NXv|-NX|-XN|-N|-X|-v)
            ;;
        -*)
            printf 'ldconfig compatibility: unsupported option: %s\n' "$argument" >&2
            exit 2
            ;;
        *)
            directories="$directories $argument"
            ;;
    esac
done

for directory in $directories; do
    [ -d "$directory" ] || continue
    for library in "$directory"/libicu*.so "$directory"/libicu*.so.*; do
        [ -f "$library" ] || continue
        printf '%s (compat) => %s\n' "${library##*/}" "$library"
    done
done

exit 0
