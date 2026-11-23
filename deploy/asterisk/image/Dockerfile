# Asterisk for the AI telephone (docs/20 §2), built from the official source
# tarball (#775). 22.12.0 releases the lock of the media channel when its
# WebSocket to `voice` fails (Asterisk issue #2121); 22.11.0, the last vendor
# image, can crash with every live call after such a failure.
#
# Every download is pinned by checksum: the tarball (also signed by the
# Asterisk Development Team, key F2FC 93DB 7587 BD1F B49E 045A 5D98 4BE3 3719
# 1CE7, checked when the pin was set) and the pjproject tarball the build
# bundles. The base is the vendor image's Alpine, so render.sh finds the same
# busybox nslookup and musl getent it was written against.
#
# Update: set ASTERISK_VERSION and both checksums (downloads.asterisk.org
# publishes asterisk-<version>.sha256; PJPROJECT_VERSION is in the tarball's
# third-party/versions.mak), compare patches/ with Alpine's main/asterisk,
# build, run the drills (docs/20 §10).
# Both stages on the same base, written out for Dependabot.
FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf AS build
ARG ASTERISK_VERSION=22.12.0
ARG ASTERISK_SHA256=f8b2ba98aac6c4ff3376bc0a2868bea6ed577ca684efb7ec739ae6e8ce6e35e2
ARG PJPROJECT_VERSION=2.17
ARG PJPROJECT_SHA256=04b2eb1f0f01aa0ad1945b167171843448a51aa6b7c3e806496d434f13a112b7
RUN apk add --no-cache build-base linux-headers bsd-compat-headers bash patch \
        bzip2 curl perl libedit-dev jansson-dev libxml2-dev sqlite-dev \
        util-linux-dev openssl-dev curl-dev unbound-dev
WORKDIR /src
RUN curl -fsSL -o asterisk.tar.gz \
        "https://downloads.asterisk.org/pub/telephony/asterisk/releases/asterisk-${ASTERISK_VERSION}.tar.gz" \
    && echo "${ASTERISK_SHA256}  asterisk.tar.gz" | sha256sum -c - \
    && mkdir -p /externals \
    && curl -fsSL -o "/externals/pjproject-${PJPROJECT_VERSION}.tar.bz2" \
        "https://raw.githubusercontent.com/asterisk/third-party/master/pjproject/${PJPROJECT_VERSION}/pjproject-${PJPROJECT_VERSION}.tar.bz2" \
    && echo "${PJPROJECT_SHA256}  /externals/pjproject-${PJPROJECT_VERSION}.tar.bz2" | sha256sum -c - \
    && tar -xzf asterisk.tar.gz --strip-components=1 \
    && rm asterisk.tar.gz
# Asterisk is written for glibc. The patches are Alpine's own for its asterisk
# package, the build the vendor image ships (aports commit
# 288f32506c726174216cec365605ec9c9570841f, main/asterisk, sha512 as there):
# above all 10-musl-mutex-init, without which the loader deadlocks on its first
# module, as musl has no static recursive mutex. As there too: no XML
# documentation (the configuration framework then needs none), modules never
# unloaded from memory, and libssl linked to asteriskssl, which reaches it by
# dlsym only.
COPY patches/ /patches/
RUN for p in /patches/*.patch; do patch -p1 < "$p"; done \
    && sed -i -e 's/ASTSSL_LIBS:=$(OPENSSL_LIB)/ASTSSL_LIBS:=-Wl,--no-as-needed $(OPENSSL_LIB) -Wl,--as-needed/g' main/Makefile
# No sounds or music on hold: the dialplan plays only the announcements of
# `voice` (/announcements) and tones. BUILD_NATIVE off: the image runs on
# whatever CPU the server has, not only on the one that built it. /var/run is a
# link to /run in Alpine, so what make installs there moves to /run.
RUN ./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
        --libdir=/usr/lib --disable-xmldoc --enable-permanent-dlopen \
        --with-pjproject-bundled --with-jansson --with-externals-cache=/externals \
    && make menuselect.makeopts \
    && menuselect/menuselect \
        --disable BUILD_NATIVE \
        --disable-category MENUSELECT_CORE_SOUNDS \
        --disable-category MENUSELECT_MOH \
        --disable-category MENUSELECT_EXTRA_SOUNDS \
        menuselect.makeopts \
    && make -j"$(nproc)" \
    && make install DESTDIR=/out \
    && rm -rf /out/usr/include \
    && mkdir -p /out/run && cp -a /out/var/run/. /out/run/ && rm -rf /out/var/run \
    && find /out/usr/sbin /out/usr/lib -type f \( -name 'asterisk' -o -name '*.so*' \) \
        -exec strip --strip-unneeded {} +

FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf
# Runtime libraries of the modules built above; curl for the healthcheck and
# the dialplan's CURL(); busybox, musl-utils (getent) and gettext-envsubst give
# render.sh its tools. unbound-libs: res_resolver_unbound, without which PJSIP
# looks up no SRV records and a trunk reached by them gets no team leg
# (render.sh, docs/20 §3). Left out against the vendor image, as nothing here
# loads them: ODBC, PostgreSQL, LDAP, Lua, Speex, Opus, fax, SRTP, Bluetooth and
# console channels.
RUN apk add --no-cache libedit jansson libxml2 sqlite-libs libuuid libcurl \
        unbound-libs libssl3 libcrypto3 libstdc++ libgcc curl ca-certificates \
        musl-utils gettext-envsubst \
    && addgroup -S -g 1000 asterisk \
    && adduser -S -D -H -u 1000 -G asterisk -h /var/lib/asterisk asterisk
COPY --from=build /out/ /
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN mkdir -p /etc/asterisk /var/run/asterisk \
    && chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk \
        /var/log/asterisk /var/spool/asterisk /var/run/asterisk \
    && chmod 0755 /usr/local/bin/entrypoint.sh \
    && asterisk -V
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/sbin/asterisk", "-f", "-T", "-W", "-U", "asterisk", "-p"]
