FROM docker.io/gentoo/stage3:amd64-openrc-20250825

# Sync portage tree - webrsync first (faster), then full sync for latest
RUN emerge-webrsync && emerge --sync

# Install Python and dependencies for the API
# Using pip since we want specific versions, not whatever's in portage
RUN emerge -q dev-lang/python dev-python/pip && \
    pip install --break-system-packages fastapi uvicorn python-multipart

# Create app directory
WORKDIR /app

# Copy application code
COPY api.py builder.py ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Binpkg cache directory
VOLUME /var/cache/binpkgs

# Config cache directory (stores uploaded portage configs)
VOLUME /var/cache/e4e/configs

EXPOSE 8443

ENTRYPOINT ["/entrypoint.sh"]
