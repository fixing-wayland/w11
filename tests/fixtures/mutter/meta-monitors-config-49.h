/* Excerpt: mutter 49.7 src/backends/meta-monitor-config-manager.h (GPL-2.0-or-later,
   Copyright (C) 2016 Red Hat), fetched 2026-09-11 from
   https://gitlab.gnome.org/GNOME/mutter/-/raw/49.7/src/backends/meta-monitor-config-manager.h
   (whole-header sha256 702270a666c3c6f428ea118284c9d825bdde6234ef8351ac483bb843ccef5c2b,
   which is byte for byte the sha256 of 50.1's header: 49 and 50 ship the SAME struct and
   differ in soname and typelib version only).  Kept as its own fixture because the record
   for 49 was measured on a live libmutter-17 (fedora43-gnome golden, GNOME Shell 49.9,
   build c7c9add6935d) and this is the upstream-source half of that same answer -- the
   Fedora rpm carries no header, so it comes from the release tag rather than the guest.
   Used by tests/test_gnome_overlap.py against gnome/overlap-typelib/gen-gir.py. */
struct _MetaMonitorsConfig
{
  GObject parent;

  MetaMonitorsConfig *parent_config;
  MetaMonitorsConfigKey *key;
  GList *logical_monitor_configs;

  GList *disabled_monitor_specs;
  GList *for_lease_monitor_specs;

  MetaMonitorsConfigFlag flags;

  MetaLogicalMonitorLayoutMode layout_mode;

  MetaMonitorSwitchConfigType switch_config;
};
