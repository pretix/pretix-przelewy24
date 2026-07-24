FROM pretix/standalone:stable
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends gettext && \
    rm -rf /var/lib/apt/lists/*
COPY . /plugin
RUN pip3 install -e /plugin && \
    cd /plugin/pretix_przelewy24 && django-admin compilemessages && \
    cd /pretix/src && sudo -u pretixuser make production
USER pretixuser
