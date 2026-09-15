// SPDX-License-Identifier: GPL-2.0-only
/*
 * w11 route-6 native-toplevel geometry emit -- an LD_PRELOAD shim into an
 * UNMODIFIED labwc.
 *
 * The problem.  On the wlr floor (labwc, and the labwc-backed Budgie /
 * Xfce-on-Wayland / LXQt-on-Wayland sessions) w11's window backend answers an
 * XWayland window's real rectangle over the X plane (route 5), but a NATIVE
 * Wayland toplevel had no rectangle at all: zwlr_foreign_toplevel_management_v1
 * and ext_foreign_toplevel_list_v1 carry a title and an app id and NO geometry,
 * and no X server has heard of the window.  So `wdotool getwindowgeometry` on a
 * native window answered `0,0 out_w x out_h` -- the whole output.
 *
 * The rule (AGENTS.md): we never fork, patch or rebuild labwc.  Everything w11
 * adds ships in w11's own package and reaches the compositor from OUTSIDE.  So
 * this is our code, in our package, loaded into the distro's bit-for-bit labwc
 * via LD_PRELOAD -- the w11-labwc session entry runs
 * `env LD_PRELOAD=/usr/lib/w11/w11-labwc-shim.so labwc`.
 *
 * The mechanism.  labwc links libwlroots-0.19 dynamically, so wlroots' exported
 * symbols are interposable.  labwc builds each view's scene tree with
 *
 *     struct wlr_scene_tree *wlr_scene_xdg_surface_create(parent, xdg_surface);
 *
 * whose returned node's origin -- wlroots guarantees it -- "will match the
 * top-left corner of the xdg_surface window geometry".  That single call ties a
 * scene node to an xdg_surface, which is the correlation the forked labwc did
 * inside its own `struct view` and the hard part of doing this from outside:
 *
 *   * identity  -- xdg_surface->toplevel->title / ->app_id (the same strings
 *                  labwc hands wlr_foreign_toplevel_handle_v1_set_title/_app_id,
 *                  so the join key the reader folds on is byte-identical);
 *   * rectangle -- wlr_scene_node_coords(&tree->node, &lx, &ly) is the absolute
 *                  on-screen top-left of the window geometry (== labwc's
 *                  view->current.x/y), and xdg_surface->geometry.{width,height}
 *                  is its size.  This is exactly the box the forked labwc wrote
 *                  and the box getwindowgeometry prints for an XWayland view;
 *   * pid       -- wl_client_get_credentials() of the surface's client.
 *
 * We interpose two symbols and listen to one signal:
 *
 *   * wlr_scene_xdg_surface_create -- register each toplevel view (skip popups),
 *     and add a destroy listener to its scene node so the row goes when the
 *     view unmaps or closes (no need to interpose destroy);
 *   * wlr_scene_output_build_state -- called once per output per composited
 *     frame, i.e. exactly when the scene (position, size, title, map/unmap) has
 *     changed.  We rebuild the file there and write only when its bytes differ
 *     from the last write (content-debounced), so a static desktop writes
 *     nothing and a move/resize/rename is on disk within one frame -- which is
 *     what makes the resize case correct without a resize-specific hook.
 *
 * The contract (unchanged; hacks/window/backend_wlr.py reads it).  One line per
 * currently-displayed toplevel view, tab-separated, written atomically
 * (temp + rename) to $XDG_RUNTIME_DIR/w11-labwc-geometry:
 *
 *     <pid>\t<x>\t<y>\t<w>\t<h>\t<app_id>\t<title>
 *
 * x/y/w/h are the view's on-screen window-geometry rectangle.  Tabs, newlines
 * and carriage returns in app_id/title become spaces so the line format is
 * stable; title is the last field.  WlrBackend._labwc_geometry folds the
 * rect+pid onto the native rows by (app_id, title), a key two windows share
 * being dropped so a tie keeps the floor.
 *
 * Everything degrades to a no-op: an unset XDG_RUNTIME_DIR, a failed write, a
 * missing symbol -- labwc runs exactly as it would without us; the reader then
 * simply finds no file and every native row keeps the floor.
 *
 * Built against the distro's libwlroots-0.19 dev headers (see build-shim.sh).
 * The struct offsets it reads are the 0.19.x ABI, the same soname labwc's own
 * libwlroots-0.19.so exports, so the compiled .so matches the running library.
 */

#define _GNU_SOURCE
#define WLR_USE_UNSTABLE

#include <dlfcn.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/box.h>

#define GEOMETRY_FILE "w11-labwc-geometry"

/* One tracked toplevel view: the scene tree wlr_scene_xdg_surface_create
 * returned, the xdg_surface it wraps (for identity/geometry/pid at write time),
 * and the destroy listener that removes this entry when the node goes. */
struct w11_view {
	struct wl_list link;
	struct wlr_scene_tree *tree;
	struct wlr_xdg_surface *xdg;
	struct wl_listener destroy;
};

static struct wl_list w11_views;
static bool w11_ready;

/* The bytes of the last successful write, so a composited frame that changed
 * nothing about any toplevel's rectangle/identity writes nothing. */
static char *w11_last;
static size_t w11_last_len;

static void
w11_init(void)
{
	if (!w11_ready) {
		wl_list_init(&w11_views);
		w11_ready = true;
	}
}

/* Copy src into dst[0..n-1], NUL-terminated, turning tab/newline/CR into a
 * space so the tab-separated, newline-delimited line format stays stable. */
static void
w11_sanitize(char *dst, const char *src, size_t n)
{
	size_t i = 0;
	if (src) {
		for (; src[i] && i + 1 < n; i++) {
			char c = src[i];
			dst[i] = (c == '\t' || c == '\n' || c == '\r') ? ' ' : c;
		}
	}
	dst[i] = '\0';
}

/* Rebuild the file's contents from the registry and, if they differ from the
 * last write, write them atomically (temp + rename).  A no-op with no
 * XDG_RUNTIME_DIR or on any I/O failure -- labwc is never disturbed. */
static void
w11_publish(void)
{
	if (!w11_ready) {
		return;
	}
	const char *dir = getenv("XDG_RUNTIME_DIR");
	if (!dir || !dir[0]) {
		return;
	}

	char *buf = NULL;
	size_t len = 0;
	FILE *mem = open_memstream(&buf, &len);
	if (!mem) {
		return;
	}

	struct w11_view *v;
	wl_list_for_each(v, &w11_views, link) {
		struct wlr_xdg_surface *xdg = v->xdg;
		if (!xdg || xdg->role != WLR_XDG_SURFACE_ROLE_TOPLEVEL
				|| !xdg->toplevel) {
			continue;
		}
		int lx = 0, ly = 0;
		/* False for a node that is not currently displayed (unmapped or
		 * minimized): no on-screen rectangle, so keep the floor for it. */
		if (!wlr_scene_node_coords(&v->tree->node, &lx, &ly)) {
			continue;
		}
		struct wlr_box g = xdg->geometry;
		if (g.width <= 0 || g.height <= 0) {
			continue;	/* not sized yet */
		}

		pid_t pid = -1;
		if (xdg->resource) {
			struct wl_client *c = wl_resource_get_client(xdg->resource);
			if (c) {
				wl_client_get_credentials(c, &pid, NULL, NULL);
			}
		}

		char app_id[256];
		char title[512];
		w11_sanitize(app_id, xdg->toplevel->app_id, sizeof(app_id));
		w11_sanitize(title, xdg->toplevel->title, sizeof(title));

		fprintf(mem, "%d\t%d\t%d\t%d\t%d\t%s\t%s\n",
			(int)pid, lx, ly, g.width, g.height, app_id, title);
	}

	if (fclose(mem) != 0) {
		free(buf);
		return;
	}

	/* Content-debounce: nothing changed since the last write. */
	if (w11_last && len == w11_last_len
			&& memcmp(buf, w11_last, len) == 0) {
		free(buf);
		return;
	}

	char path[PATH_MAX];
	char tmp[PATH_MAX];
	if (snprintf(path, sizeof(path), "%s/%s", dir, GEOMETRY_FILE)
			>= (int)sizeof(path)) {
		free(buf);
		return;
	}
	if (snprintf(tmp, sizeof(tmp), "%s.tmp.%d", path, (int)getpid())
			>= (int)sizeof(tmp)) {
		free(buf);
		return;
	}

	FILE *f = fopen(tmp, "we");
	if (!f) {
		free(buf);
		return;
	}
	bool ok = (len == 0) || (fwrite(buf, 1, len, f) == len);
	if (fclose(f) != 0 || !ok) {
		unlink(tmp);
		free(buf);
		return;
	}
	if (rename(tmp, path) != 0) {
		unlink(tmp);
		free(buf);
		return;
	}

	free(w11_last);
	w11_last = buf;
	w11_last_len = len;
}

static void
w11_on_destroy(struct wl_listener *listener, void *data)
{
	(void)data;
	struct w11_view *v = wl_container_of(listener, v, destroy);
	wl_list_remove(&v->destroy.link);
	wl_list_remove(&v->link);
	free(v);
	/* The unmap/close that destroyed the node also damages the scene, so the
	 * next build_state re-emits without this row; no explicit write here. */
}

/* --- interposed wlroots symbols ------------------------------------------- */

struct wlr_scene_tree *
wlr_scene_xdg_surface_create(struct wlr_scene_tree *parent,
		struct wlr_xdg_surface *xdg_surface)
{
	static struct wlr_scene_tree *(*real)(struct wlr_scene_tree *,
		struct wlr_xdg_surface *);
	if (!real) {
		real = dlsym(RTLD_NEXT, "wlr_scene_xdg_surface_create");
	}
	struct wlr_scene_tree *tree = real ? real(parent, xdg_surface) : NULL;

	w11_init();
	/* Only the set the foreign-toplevel protocols advertise: toplevels, not
	 * popups (labwc builds those with this same call). */
	if (tree && xdg_surface
			&& xdg_surface->role == WLR_XDG_SURFACE_ROLE_TOPLEVEL) {
		struct w11_view *v = calloc(1, sizeof(*v));
		if (v) {
			v->tree = tree;
			v->xdg = xdg_surface;
			v->destroy.notify = w11_on_destroy;
			wl_signal_add(&tree->node.events.destroy, &v->destroy);
			wl_list_insert(&w11_views, &v->link);
		}
	}
	return tree;
}

bool
wlr_scene_output_build_state(struct wlr_scene_output *scene_output,
		struct wlr_output_state *state,
		const struct wlr_scene_output_state_options *options)
{
	static bool (*real)(struct wlr_scene_output *, struct wlr_output_state *,
		const struct wlr_scene_output_state_options *);
	if (!real) {
		real = dlsym(RTLD_NEXT, "wlr_scene_output_build_state");
	}
	bool ret = real ? real(scene_output, state, options) : false;
	w11_publish();
	return ret;
}
