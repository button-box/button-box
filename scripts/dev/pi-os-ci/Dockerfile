# Boot the unmodified Raspberry Pi OS Lite rootfs under systemd in a container.
# Changes here are limited to what a container needs; see docs/ci-pi-image.md.
ARG ROOTFS=button-box-ci/pi-os-rootfs:latest
FROM ${ROOTFS}
ARG ADMIN=admin
ENV container=docker

# No SD card partitions, swap file, first-boot wizard, or consoles. Docker
# configures the network, so nothing waits two minutes for networkd.
RUN : >/etc/fstab \
 && for unit in userconfig.service sshswitch.service regenerate_ssh_host_keys.service \
      rpi-eeprom-update.service dphys-swapfile.service systemd-remount-fs.service \
      getty@tty1.service serial-getty@ttyAMA0.service serial-getty@ttyS0.service \
      systemd-firstboot.service systemd-growfs-root.service \
      systemd-networkd-wait-online.service; do \
      ln -sf /dev/null "/etc/systemd/system/$unit"; \
    done

# The non-root sudo administrator that Raspberry Pi Imager creates, with the
# groups and passwordless sudo that Pi OS gives its first user.
RUN groups="" \
 && for group in adm dialout cdrom sudo audio video plugdev games users input render netdev gpio i2c spi; do \
      if getent group "$group" >/dev/null; then groups="$groups,$group"; fi; \
    done \
 && useradd --create-home --shell /bin/bash --groups "${groups#,}" "$ADMIN" \
 && printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$ADMIN" >"/etc/sudoers.d/010_$ADMIN-nopasswd" \
 && chmod 0440 "/etc/sudoers.d/010_$ADMIN-nopasswd"

COPY rootfs/ /
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
