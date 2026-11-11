# Test bed for other GNOME Shell versions: headless gnome-shell (Wayland,
# software rendering) on Fedora. Used only by tests/gnome/e2e-gnome-in-container.sh.
#   fedora:39 -> GNOME 45, fedora:41 -> GNOME 47, fedora:42 -> GNOME 48
ARG BASE=fedora:41
FROM ${BASE}
ARG BASE
RUN if [ "${BASE}" = "fedora:39" ]; then \
      sed -i -e 's|^metalink=|#metalink=|' -e 's|^#baseurl=http://download.example/pub/fedora/linux|baseurl=https://archives.fedoraproject.org/pub/archive/fedora/linux|' /etc/yum.repos.d/*.repo; \
    fi; \
    dnf -y install --setopt=install_weak_deps=False gnome-shell mutter gnome-settings-daemon gsettings-desktop-schemas \
      python3-gobject python3-pyqt6 qt6-qtwayland gtk4 dbus-daemon glib2 mesa-dri-drivers mesa-libEGL procps-ng gcc libxcb-devel \
      gnome-shell-extension-appindicator >/tmp/dnf.log 2>&1 || (tail -30 /tmp/dnf.log; exit 1)
RUN useradd -m tester
