#!/bin/bash
# The golden image of one flavor, from the cache or built here: what the VM jobs of
# .github/workflows/ci.yml run before vm/live-smoke.sh.
#
#     scripts/ci-golden.sh <flavor>
#
# A golden is keyed by the sha256 of everything that goes into building it (the flavor
# yaml, vm/vmctl, the three build scripts, and vm/golden-epoch, a file to bump by hand
# when the upstream cloud image or ISO has moved under an unchanged recipe) and kept as
# an OCI artifact at ghcr.io/<owner>/w11-golden/<flavor>:<key>, pushed with oras.
# On a hit it is pulled into $VMDATA/golden/<flavor>.qcow2 in about a minute; on a miss
# the base image or ISO is downloaded into $VMIMAGES, the golden is built the way
# vm/README.md says (vmctl build, build-iso-golden.sh for an ISO flavor, or
# build-nixos-golden.sh for a `# vmctl-nix:` one), flattened and compressed into a
# standalone qcow2 (no backing file, so the cache does not depend on a path), and pushed
# for the next run.  A `# vmctl-nix:` flavor is the exception at both ends: its image has
# the rig's root key baked in, so it is neither pulled nor pushed -- see the paragraph
# above `cacheable=1` below.
#
#   GOLDEN_REPO   the OCI repository prefix (default ghcr.io/<GITHUB_REPOSITORY_OWNER>/w11-golden)
#   GOLDEN_PUSH   set to 0 to never push (default 1 when a registry login exists)
#   VMDATA, VMIMAGES   as vm/vmctl reads them
set -eu
flavor=${1:?usage: scripts/ci-golden.sh <flavor>}
cd "$(dirname "$0")/.."
VMDATA=${VMDATA:-$HOME/vm-data}; VMIMAGES=${VMIMAGES:-$HOME/images}
export VMDATA VMIMAGES
yaml=vm/flavors/$flavor.yaml; [ -f "$yaml" ] || { echo "no flavor $flavor" >&2; exit 2; }
hdr() { sed -n "s/^#[[:space:]]*$1:[[:space:]]*//p" "$yaml" | head -1; }
iso=$(hdr vmctl-iso)
base=$(hdr vmctl-base)
nix=$(hdr vmctl-nix)
base_sha=$(hdr vmctl-base-sha256)
owner=$(printf '%s' "${GITHUB_REPOSITORY_OWNER:-$(git config --get remote.origin.url | sed 's|.*[:/]\([^/]*\)/[^/]*$|\1|')}" | tr 'A-Z' 'a-z')
repo=${GOLDEN_REPO:-ghcr.io/$owner/w11-golden}

key_inputs() {   # the files whose bytes decide this flavor's cache key, one per line
    # A NixOS golden runs none of the three cloud-image builders -- vm/build-nixos-golden.sh
    # asks nix for `images.<attr>` and nix reads the flake -- so hashing build-image.sh
    # there would rebuild every NixOS image for an Ubuntu-only change and, worse, would
    # NOT rebuild one when vm/nixos/common.nix moved.  The two lists share only the yaml
    # and vm/golden-epoch, the hand-bumped file that says "the upstream moved under an
    # unchanged recipe".
    #
    # The nix list is NOT sufficient to identify one of these images and is not used as
    # if it were: vm/nixos/flake.nix takes the repository itself as
    # `inputs.w11.url = "path:../.."` and the module installs the package built
    # from it, so the image's contents move with any file of the tree.  Nothing serves a
    # stale one today because a `# vmctl-nix:` golden is neither pulled nor pushed (see
    # `cacheable` below) -- the key is only printed in the `building $ref` line.  The day
    # a repository-held key pair turns that cache on, this list needs the package source
    # in it (`git ls-files | LC_ALL=C sort`, or the derivation path nix computes).
    #
    # `find`, not `ls vm/nixos/* nix/*`: nix/checks IS a directory, and `ls` on a
    # directory prints a `nix/checks:` header and then bare file names, so the six
    # paths that followed did not exist as written -- `xargs cat` printed six
    # "No such file or directory" lines into every nixos rig job's log and hashed
    # the tree WITHOUT nix/checks/*.  `-type f` and a stable sort instead.
    if [ -n "$nix" ]; then
        echo "$yaml"; echo vm/build-nixos-golden.sh
        find vm/nixos nix -type f | LC_ALL=C sort
        echo flake.nix; echo flake.lock; echo vm/golden-epoch
    else
        echo "$yaml"; echo vm/vmctl; echo vm/build-image.sh
        echo vm/build-iso-golden.sh; echo vm/build-iso-image.sh; echo vm/golden-epoch
    fi
}
# xargs cat, not `cat $(...)`: the list is one path per line and no path in this tree has
# a space in it, but the order is the function's and must not be re-sorted -- the key of
# every cloud-image flavor in GHCR today was computed over exactly these six files in
# exactly this order.
key=$(key_inputs | xargs cat | sha256sum | cut -c1-16)
ref=$repo/$flavor:$key
golden=$VMDATA/golden/$flavor.qcow2
mkdir -p "$VMDATA/golden" "$VMIMAGES"
say() { printf '\nci-golden %s: %s\n' "$flavor" "$*"; }

# A NixOS golden carries the rig's ROOT SSH KEY in its own /etc: vm/nixos/common.nix reads
# it with `builtins.getEnv "VMCTL_ROOT_PUBKEY"` and the build is --impure, so the image is
# a function of the machine that built it.  Pull one built elsewhere and `vmctl ssh` has no
# way in -- NixOS runs no cloud-init, so the seed vmctl attaches is read by nobody
# [recon2/nixos 7.1].  So these are built every time, ~10 min, and not pushed.  The route
# to caching them is one key pair held by the repository and handed to every runner, which
# means a secret and is the owner's call; until then this is a rebuild and not a gap in
# what CI proves.
cacheable=1
if [ -n "$nix" ]; then
    cacheable=
    keyfile=$VMDATA/keys/id_ed25519
    if [ ! -f "$keyfile.pub" ]; then
        say "generating the rig root key at $keyfile (build-nixos-golden.sh bakes it in)"
        mkdir -p "$VMDATA/keys"
        ssh-keygen -t ed25519 -N "" -f "$keyfile" >/dev/null
    fi
fi

if [ -f "$golden" ]; then say "already at $golden"; exit 0; fi
if [ -n "$cacheable" ] && command -v oras >/dev/null && oras manifest fetch "$ref" >/dev/null 2>&1; then
    say "cache hit $ref"
    ( cd "$VMDATA/golden" && oras pull "$ref" >/dev/null )
    [ -f "$golden" ] || { echo "pulled $ref but $golden is not there" >&2; ls -la "$VMDATA/golden" >&2; exit 1; }
    qemu-img info "$golden" | sed 's/^/    /'; exit 0
fi
if [ -n "$cacheable" ]; then say "cache miss $ref: building"
else say "not cached (see above): building $ref here"; fi

# -- the base image or the ISO ---------------------------------------------------------
check_sha256() {   # <file> <expected sha256> -- the ISO builder's check, for a base image
    got=$(sha256sum "$VMIMAGES/$1" | cut -d' ' -f1)
    [ "$got" = "$2" ] || {
        echo "ci-golden: sha256 mismatch on $VMIMAGES/$1: got $got, want $2" >&2
        exit 1
    }
    say "sha256 ok $1"
}
fetch() {   # <file> -> $VMIMAGES/<file>
    # Keyed on the file name, as vm/flavors/*.yaml writes it.  The two foreign bases were
    # measured answering on 2026-09-08: Fedora 44's tree at dl.fedoraproject.org
    # [recon2/fedora.md 2] and the Arch cloud image at geo.mirror.pkgbuild.com
    # [recon2/arch.md 1].  A name with no rule exits 2 rather than guessing a URL.
    local f=$1 url sha rel ver
    [ -s "$VMIMAGES/$f" ] && return 0
    case $f in
        noble-server-cloudimg-amd64.img)        url=https://cloud-images.ubuntu.com/noble/current/$f ;;
        ubuntu-26.04-server-cloudimg-amd64.img) url=https://cloud-images.ubuntu.com/releases/26.04/release/$f ;;
        stonking-server-cloudimg-amd64.img)     url=https://cloud-images.ubuntu.com/stonking/current/$f ;;
        ubuntu-24.04.*-desktop-amd64.iso)       url=https://releases.ubuntu.com/24.04/$f ;;
        ubuntu-26.04.*-desktop-amd64.iso)       url=https://releases.ubuntu.com/26.04/$f ;;
        # Fedora-Cloud-Base-Generic-<rel>-<compose>.x86_64.qcow2; the release number is the
        # directory.  download.fedoraproject.org 302s to a mirror and dl. serves it
        # directly; the direct one is what was measured, so it is what is written here.
        Fedora-Cloud-Base-Generic-*.x86_64.qcow2)
            rel=${f#Fedora-Cloud-Base-Generic-}; rel=${rel%%-*}
            url=https://dl.fedoraproject.org/pub/fedora/linux/releases/$rel/Cloud/x86_64/images/$f ;;
        # Arch-Linux-x86_64-cloudimg-<date>.<build>.qcow2, the DATED arch-boxes build: a
        # rolling golden keyed on `latest/` would rot weekly, so vm/flavors/arch-*.yaml pin
        # a build and the mirror keeps each one under images/v<date>.<build>/.  Its
        # published .SHA256 sits beside it and is checked here whatever the yaml says.
        Arch-Linux-x86_64-cloudimg-*.qcow2)
            ver=${f#Arch-Linux-x86_64-cloudimg-}; ver=${ver%.qcow2}
            url=https://geo.mirror.pkgbuild.com/images/v$ver/$f ;;
        *) echo "ci-golden: no download rule for $f" >&2; exit 2 ;;
    esac
    say "downloading $url"
    # `curl ... && mv ...` was the old shape.  It did stop the run -- the && list was the
    # function's last command, so fetch returned curl's status and `set -e` fired at the
    # call site (measured: `set -e; f() { false && echo mv; }; f; echo after` prints
    # nothing and exits 1) -- but it stopped it with no message beyond curl's own, and a
    # transfer cut off part way left `<file>.part` in $VMIMAGES for the next job to keep
    # (nothing ever removes it; `[ -s "$VMIMAGES/$f" ]` only guards the final name).
    # This names the URL that failed and takes the partial file with it.
    curl -fL --retry 3 -o "$VMIMAGES/$f.part" "$url" || {
        rm -f "$VMIMAGES/$f.part"
        echo "ci-golden: download failed: $url" >&2
        exit 1
    }
    mv "$VMIMAGES/$f.part" "$VMIMAGES/$f"
    case $f in
        Arch-Linux-x86_64-cloudimg-*.qcow2)
            sha=$(curl -fsSL --retry 3 "$url.SHA256" | awk 'NR==1{print $1}')
            [ -n "$sha" ] || { echo "ci-golden: $url.SHA256 answered nothing" >&2; exit 1; }
            check_sha256 "$f" "$sha" ;;
    esac
}
t0=$(date +%s)
if [ -n "$nix" ]; then
    # No base image at all: vm/build-nixos-golden.sh builds the qcow2 out of
    # vm/nixos/<flavor>.nix with nix, and a QEMU runs inside the nix sandbox to do it
    # (~10 min cold, and 5.19 GB of raw image before the convert below) [recon2/nixos].
    bash vm/build-nixos-golden.sh "$flavor"
elif [ -n "$iso" ]; then
    fetch "$iso"; bash vm/build-iso-golden.sh "$flavor" --mem 4G
else
    fetch "$base"
    [ -z "$base_sha" ] || check_sha256 "$base" "$base_sha"
    vm/vmctl build "$flavor" --cpus 2 --mem 4G
fi
say "built in $(( ($(date +%s) - t0) / 60 )) min"

# -- flatten + compress, so the cached file stands alone ------------------------------
# The same `&&` trap fetch() documents at lines 145-151, and here it was worse: this is a
# top-level statement, not a function's last command, so a failed convert did not even
# reach `set -e` at a call site -- `set -eu; false && mv a b; echo REACHED` prints REACHED
# and exits 0 (measured).  The `mv` was skipped, the unflattened golden was printed by the
# `qemu-img info` below and pushed under this flavor's cache key, and every later run
# pulled a qcow2 whose backing file is $VMIMAGES/<base> -- a path a fresh runner does not
# have, because the base is downloaded only on a cache miss.  The half-written
# `<flavor>.qcow2.standalone` stayed in $VMDATA/golden as well; nothing ever removed it.
tmp=$golden.standalone
qemu-img convert -c -O qcow2 "$golden" "$tmp" || {
    rm -f "$tmp"
    echo "ci-golden: flatten failed for $golden" >&2
    exit 1
}
mv "$tmp" "$golden"
qemu-img info "$golden" | sed 's/^/    /'

if [ -n "$cacheable" ] && [ "${GOLDEN_PUSH:-1}" = 1 ] && command -v oras >/dev/null; then
    say "pushing $ref"
    ( cd "$VMDATA/golden" && oras push "$ref" --artifact-type application/vnd.w11.golden \
        "$flavor.qcow2:application/vnd.qemu.qcow2" ) || say "push failed (kept the local golden)"
fi
