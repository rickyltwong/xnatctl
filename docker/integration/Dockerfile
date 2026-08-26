# syntax=docker/dockerfile:1
# check=skip=SecretsUsedInArgOrEnv
#
# XNAT for the integration tier. Derived from NrgXnat/xnat-docker-compose,
# trimmed to what tests need: no nginx, no ActiveMQ, no Docker socket. The
# credentials below are fixed throwaways for a container that only ever
# listens on localhost.
FROM tomcat:9.0-jdk8-temurin-noble

ARG XNAT_VERSION
ARG XNAT_ROOT=/data/xnat
ARG XNAT_HOME=/data/xnat/home
ARG XNAT_DATASOURCE_URL=jdbc:postgresql://xnat-db/xnat
ARG XNAT_DATASOURCE_USERNAME=xnat
ARG XNAT_DATASOURCE_PASSWORD=xnat

# NrgXnat/container-service, pinned by exact tag -- never "latest". 3.7.2 is
# the newest release built against XNAT 1.9.2 (its build.gradle pins
# vXnat="1.9.2"); 3.7.3+ moved on to 1.9.3, which is a different XNAT_VERSION
# than this image defaults to. Bumping the XNAT_VERSION default later means
# re-checking that pairing, not just bumping this number to match.
ARG CONTAINER_SERVICE_VERSION=3.7.2
# sha256 of the release asset, from GitHub's release-asset `digest` field
# (`curl -s https://api.github.com/repos/NrgXnat/container-service/releases/tags/${CONTAINER_SERVICE_VERSION}`).
# GitHub computes this itself at upload time, so it is a real integrity check,
# not a value we invented.
ARG CONTAINER_SERVICE_SHA256=ff81bb3293334a90b43c3a767f5d2ee22b3df8f6e105b1a0805fb2a83e464fb6

COPY wait-for-postgres.sh /usr/local/bin/wait-for-postgres.sh

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client unzip curl \
    && rm -rf /var/lib/apt/lists/* \
    && chmod +x /usr/local/bin/wait-for-postgres.sh

RUN rm -rf ${CATALINA_HOME}/webapps/* \
    && mkdir -p \
        ${CATALINA_HOME}/webapps/ROOT \
        ${XNAT_HOME}/config \
        ${XNAT_HOME}/logs \
        ${XNAT_HOME}/plugins \
        ${XNAT_HOME}/work \
        ${XNAT_ROOT}/archive \
        ${XNAT_ROOT}/build \
        ${XNAT_ROOT}/cache \
        ${XNAT_ROOT}/ftp \
        ${XNAT_ROOT}/pipeline \
        ${XNAT_ROOT}/prearchive

# hibernate.hbm2ddl.auto=update is what builds the schema on first boot.
RUN printf '%s\n' \
    "datasource.driver=org.postgresql.Driver" \
    "datasource.url=${XNAT_DATASOURCE_URL}" \
    "datasource.username=${XNAT_DATASOURCE_USERNAME}" \
    "datasource.password=${XNAT_DATASOURCE_PASSWORD}" \
    "hibernate.dialect=org.hibernate.dialect.PostgreSQL9Dialect" \
    "hibernate.hbm2ddl.auto=update" \
    "hibernate.show_sql=false" \
    "hibernate.cache.use_second_level_cache=true" \
    "hibernate.cache.use_query_cache=true" \
    "spring.http.multipart.max-file-size=1073741824" \
    "spring.http.multipart.max-request-size=1073741824" \
    > ${XNAT_HOME}/config/xnat-conf.properties \
    && printf '%s\n' "[siteConfig]" "adminEmail=integration@example.invalid" \
    > ${XNAT_HOME}/config/prefs-init.ini

# The official release artifact. Pinned by tag so a rebuild is reproducible.
RUN curl --fail --location --silent \
        --output /tmp/xnat-web.war \
        "https://api.bitbucket.org/2.0/repositories/xnatdev/xnat-web/downloads/xnat-web-${XNAT_VERSION}.war" \
    && unzip -q -o -d ${CATALINA_HOME}/webapps/ROOT /tmp/xnat-web.war \
    && rm -f /tmp/xnat-web.war

# The Container Service plugin. Without this, ${XNAT_HOME}/plugins stays
# empty and the image boots without it -- which silently forecloses any
# automated verification of Container Service behaviour, not just manual
# use. Fail the build outright rather than shipping an image with a missing
# or truncated jar: `--fail` makes a 404/redirect-to-error-page a build
# failure instead of a small HTML file silently landing in plugins/, and the
# sha256 check catches a corrupt or substituted download that still happens
# to be non-empty.
RUN curl --fail --location --silent \
        --output "${XNAT_HOME}/plugins/container-service-${CONTAINER_SERVICE_VERSION}.jar" \
        "https://github.com/NrgXnat/container-service/releases/download/${CONTAINER_SERVICE_VERSION}/container-service-${CONTAINER_SERVICE_VERSION}-fat.jar" \
    && echo "${CONTAINER_SERVICE_SHA256}  ${XNAT_HOME}/plugins/container-service-${CONTAINER_SERVICE_VERSION}.jar" | sha256sum -c - \
    && test -s "${XNAT_HOME}/plugins/container-service-${CONTAINER_SERVICE_VERSION}.jar"

ENV XNAT_HOME=${XNAT_HOME} \
    XNAT_DATASOURCE_USERNAME=${XNAT_DATASOURCE_USERNAME} \
    PGPASSWORD=${XNAT_DATASOURCE_PASSWORD}

EXPOSE 8080
CMD ["wait-for-postgres.sh", "/usr/local/tomcat/bin/catalina.sh", "run"]
