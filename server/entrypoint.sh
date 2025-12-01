#!/bin/bash
set -e

# Create required directories
mkdir -p /var/cache/binpkgs
mkdir -p /var/cache/e4e/configs
mkdir -p /var/cache/e4e/artifacts

# Set up binpkg settings in make.conf if not present
if ! grep -q "FEATURES.*buildpkg" /etc/portage/make.conf 2>/dev/null; then
  echo 'FEATURES="${FEATURES} buildpkg"' >>/etc/portage/make.conf
fi

# Ensure PKGDIR is set
if ! grep -q "PKGDIR" /etc/portage/make.conf 2>/dev/null; then
  echo 'PKGDIR="/var/cache/binpkgs"' >>/etc/portage/make.conf
fi

# Set reasonable MAKEOPTS if not configured
if ! grep -q "MAKEOPTS" /etc/portage/make.conf 2>/dev/null; then
  JOBS=$(nproc)
  echo "MAKEOPTS=\"-j${JOBS}\"" >>/etc/portage/make.conf
fi

echo "e4e-builder starting..."
echo "  PKGDIR: /var/cache/binpkgs"
echo "  Config cache: /var/cache/e4e/configs"
echo "  Artifact cache: /var/cache/e4e/artifacts"
echo ""

exec uvicorn api:app --host 0.0.0.0 --port 8443
