FROM scratch
COPY --chown=65532:65532 tenant-admin /tenant-admin
COPY --chown=65532:65532 web /web
USER 65532:65532
ENTRYPOINT ["/tenant-admin"]
