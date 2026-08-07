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

ENV XNAT_HOME=${XNAT_HOME} \
    XNAT_DATASOURCE_USERNAME=${XNAT_DATASOURCE_USERNAME} \
    PGPASSWORD=${XNAT_DATASOURCE_PASSWORD}

EXPOSE 8080
CMD ["wait-for-postgres.sh", "/usr/local/tomcat/bin/catalina.sh", "run"]
