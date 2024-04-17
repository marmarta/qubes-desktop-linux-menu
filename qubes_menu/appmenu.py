#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Main Application Menu class and helpers.
"""
# pylint: disable=import-error
import asyncio
import os
import subprocess
import sys
from typing import Optional, Dict
import pkg_resources
import logging

import qubesadmin
import qubesadmin.events

from .settings_page import SettingsPage
from .application_page import AppPage
from .search_page import SearchPage
from .desktop_file_manager import DesktopFileManager
from .favorites_page import FavoritesPage
from .custom_widgets import SelfAwareMenu
from .vm_manager import VMManager
from .page_handler import MenuPage
from .constants import INITIAL_PAGE_FEATURE, SORT_RUNNING_FEATURE

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Gio

import gbulb
gbulb.install()


logger = logging.getLogger('qubes-appmenu')

def load_theme(widget: Gtk.Widget, light_theme_path: str,
               dark_theme_path: str):
    """
    Load a dark or light theme to current screen, based on widget's
    current (system) defaults.
    :param widget: Gtk.Widget, preferably main window
    :param light_theme_path: path to file with light theme css
    :param dark_theme_path: path to file with dark theme css
    """
    path = light_theme_path if is_theme_light(widget) else dark_theme_path

    screen = Gdk.Screen.get_default()
    provider = Gtk.CssProvider()
    provider.load_from_path(path)
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def is_theme_light(widget):
    """Check if current theme is light or dark"""
    style_context: Gtk.StyleContext = widget.get_style_context()
    background_color: Gdk.RGBA = style_context.get_background_color(
        Gtk.StateType.NORMAL)
    text_color: Gdk.RGBA = style_context.get_color(
        Gtk.StateType.NORMAL)
    background_intensity = background_color.red + \
                           background_color.blue + background_color.green
    text_intensity = text_color.red + text_color.blue + text_color.green

    return text_intensity < background_intensity


class AppMenu(Gtk.Application):
    """
    Main Gtk.Application for appmenu.
    """
    def __init__(self, qapp, dispatcher):
        """
        :param qapp: qubesadmin.Qubes object
        :param dispatcher: qubesadmin.vm.EventsDispatcher
        """
        super().__init__(application_id='org.qubesos.appmenu',
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,)
        self.qapp = qapp
        self.dispatcher = dispatcher
        self.primary = False
        self.keep_visible = False
        self.initial_page = 1
        self.sort_running = False
        self.start_in_background = False

        self._add_cli_options()

        self.builder: Optional[Gtk.Builder] = None
        self.main_window: Optional[Gtk.Window] = None
        self.main_notebook: Optional[Gtk.Notebook] = None

        self.fav_app_list: Optional[Gtk.ListBox] = None
        self.sys_tools_list: Optional[Gtk.ListBox] = None

        self.desktop_file_manager: Optional[DesktopFileManager] = None
        self.vm_manager: Optional[VMManager] = None

        self.handlers: Dict[str, MenuPage] = {}

        self.power_button: Optional[Gtk.Button] = None

        self.highlight_tag: Optional[str] = None

        self.tasks = []

    def _add_cli_options(self):
        self.add_main_option(
            "keep-visible",
            ord("k"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Do not hide the menu after action",
            None,
        )

        self.add_main_option(
            'page',
            ord('p'),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.INT,
            "Open menu at selected page; 1 is the apps page 1 is the favorites "
            "page and 2 is the system tools page"
        )

        self.add_main_option(
            "background",
            ord("b"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Do not show the menu at start, run in the background; useful "
            "for initial autostart",
            None,
        )

    def do_command_line(self, command_line):
        """
        Handle CLI arguments. This method overrides default do_command_line
        from Gtk.Application (and due to pygtk being dynamically generated
        pylint is confused about its arguments).
        """
        # pylint: disable=arguments-differ
        Gtk.Application.do_command_line(self, command_line)
        options = command_line.get_options_dict()
        # convert GVariantDict -> GVariant -> dict
        options = options.end().unpack()

        if "keep-visible" in options:
            self.keep_visible = True
        if "page" in options:
            self.initial_page = options['page']
        if "background" in options:
            self.start_in_background = True
        self.activate()
        return 0

    @staticmethod
    def _do_power_button(_widget):
        """
        Run xfce4's default logout button. Possible enhancement would be
        providing our own tiny program.
        """
        # pylint: disable=consider-using-with
        current_environs = os.environ.get('XDG_CURRENT_DESKTOP', '').split(':')

        if 'KDE' in current_environs:
            dbus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            proxy = Gio.DBusProxy.new_sync(
                dbus,  # dbus
                Gio.DBusProxyFlags.DO_NOT_CONNECT_SIGNALS |
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,  # flags
                None,  # info
                "org.kde.LogoutPrompt",  # bus name
                "/LogoutPrompt",  # object_path
                "org.kde.LogoutPrompt")  # interface
            proxy.call(
                'promptLogout',  # method name
                None,  # parameters
                0,  # flags
                0  # timeout_msec
            )
        else:
            subprocess.Popen('xfce4-session-logout', stdin=subprocess.DEVNULL)

    def do_activate(self, *args, **kwargs):
        """
        Method called whenever this program is run; it executes actual setup
        only at true first start, in other cases just presenting the main window
        to user.
        """
        if not self.primary:
            self.perform_setup()
            self.primary = True
            assert self.main_window
            assert self.main_notebook
            if not self.start_in_background:
                self.main_window.show_all()
            self.initialize_state()
            # set size if too big
            current_height = self.main_window.get_allocated_height()
            max_height = self.main_window.get_screen().get_height() * 0.9
            if current_height > max_height:
                self.main_window.resize(self.main_window.get_allocated_width(),
                                        int(max_height))

            # grab a focus on the initially selected page so that keyboard
            # navigation works
            self.main_notebook.get_tab_label(
                self.main_notebook.get_nth_page(
                    self.initial_page)).get_parent().grab_focus()

            loop = asyncio.get_event_loop()
            self.tasks = [
                asyncio.ensure_future(self.dispatcher.listen_for_events()),
            ]

            loop.run_until_complete(asyncio.wait(
                self.tasks, return_when=asyncio.FIRST_EXCEPTION))
        else:
            if self.main_notebook:
                self.main_notebook.set_current_page(self.initial_page)
            if self.main_window:
                self.main_window.set_keep_above(True)
                if self.main_window.is_visible() and not self.keep_visible:
                    self.main_window.hide()
                else:
                    self.main_window.present()

    def hide_menu(self):
        """
        Unless CLI options specified differently, the menu will try to hide
        itself. Should be called after all sorts of actions like running an
        app or clicking outside the menu.
        """
        # reset search tab
        self.handlers['search_page'].initialize_page()
        if not self.keep_visible and self.main_window:
            self.main_window.hide()

    def _key_press(self, _widget, event):
        """
        Keypress handler, to allow closing the menu with an ESC key and to fix
        some issues with space (as we have search by default, we should not
        react to space with launching an app).
        """
        if event.keyval == Gdk.KEY_Escape:
            self.hide_menu()
        if event.keyval == Gdk.KEY_space:
            if not isinstance(self.get_active_window().get_focus(),
                              Gtk.SearchEntry):
                return True
        return False

    def _focus_out(self, _widget, _event: Gdk.EventFocus):
        """
        Hide the menu on focus out, unless a right-click menu is open
        """
        if SelfAwareMenu.OPEN_MENUS <= 0:
            self.hide_menu()

    def initialize_state(self):
        """
        Initial state, that is - menu is open at the 0th page and pages
        will initialize their state if needed. Separate function because
        some things (like widget size adjustments) must be called after
        widgets are realized and not on init.
        """
        for page in self.handlers.values():
            page.initialize_page()
        if self.main_notebook:
            self.main_notebook.set_current_page(self.initial_page)

    def perform_setup(self):
        """
        The function that performs actual widget realization and setup. Should
        be only called once, in the main instance of this application.
        """
        # build the frontend
        self.builder = Gtk.Builder()

        self.fav_app_list = self.builder.get_object('fav_app_list')
        self.sys_tools_list = self.builder.get_object('sys_tools_list')
        self.builder.add_from_file(pkg_resources.resource_filename(
            __name__, 'qubes-menu.glade'))
        self.main_window = self.builder.get_object('main_window')
        self.main_notebook = self.builder.get_object('main_notebook')

        self.main_window.set_events(Gdk.EventMask.FOCUS_CHANGE_MASK)
        self.main_window.connect('focus-out-event', self._focus_out)
        self.main_window.connect('key_press_event', self._key_press)
        self.add_window(self.main_window)
        self.desktop_file_manager = DesktopFileManager(self.qapp)
        self.vm_manager = VMManager(self.qapp, self.dispatcher)

        self.handlers = {
            'search_page': SearchPage(self.vm_manager, self.builder,
                                      self.desktop_file_manager),
            'app_page': AppPage(self.vm_manager, self.builder,
                                self.desktop_file_manager),
            'favorites_page': FavoritesPage(self.qapp, self.builder,
                                            self.desktop_file_manager,
                                            self.dispatcher, self.vm_manager),
            'settings_page': SettingsPage(self.qapp, self.builder,
                                          self.desktop_file_manager,
                                          self.dispatcher)}
        self.power_button = self.builder.get_object('power_button')
        self.power_button.connect('clicked', self._do_power_button)
        self.main_notebook.connect('switch-page', self._handle_page_switch)
        self.connect('shutdown', self.do_shutdown)

        self.main_window.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        self.main_window.connect('key_press_event', self._key_pressed)

        self.load_style()
        Gtk.Settings.get_default().connect('notify::gtk-theme-name',
                                           self.load_style)

        self.load_settings()

        # monitor for settings changes
        for feature in [INITIAL_PAGE_FEATURE, SORT_RUNNING_FEATURE]:
            self.dispatcher.add_handler(
                'domain-feature-set:' + feature,
                self._update_settings)
            self.dispatcher.add_handler(
                'domain-feature-delete:' + feature,
                self._update_settings)

    def load_style(self, *_args):
        """Load appropriate CSS stylesheet and associated properties."""
        load_theme(self.main_window,
                   light_theme_path=pkg_resources.resource_filename(
                       __name__, 'qubes-menu-light.css'),
                   dark_theme_path=pkg_resources.resource_filename(
                       __name__, 'qubes-menu-dark.css'))

        label = Gtk.Label()
        style_context: Gtk.StyleContext = label.get_style_context()
        style_context.add_class('search_highlight')
        bg_color = style_context.get_background_color(Gtk.StateType.NORMAL)
        fg_color = style_context.get_color(Gtk.StateType.NORMAL)

        # This converts a Gdk.RGBA color to a hex representation liked by span
        # tags in Pango
        self.highlight_tag = \
            f'<span background="{self._rgba_color_to_hex(bg_color)}" ' \
            f'color="{self._rgba_color_to_hex(fg_color)}">'

    def load_settings(self):
        """Load settings from dom0 features."""
        local_vm = self.qapp.domains[self.qapp.local_name]

        try:
            initial_page = int(local_vm.features.get(INITIAL_PAGE_FEATURE, 1))
        except ValueError:
            initial_page = 1
        self.initial_page = initial_page

        self.sort_running = \
            bool(local_vm.features.get(SORT_RUNNING_FEATURE, False))

        for handler in self.handlers.values():
            handler.set_sorting_order(self.sort_running)

    def _update_settings(self, vm, _event, **_kwargs):
        if not str(vm) == self.qapp.local_name:
            return

        self.load_settings()

    @staticmethod
    def _rgba_color_to_hex(color: Gdk.RGBA):
        return '#' + ''.join([f'{int(c*255):0>2x}'
                              for c in (color.red, color.green, color.blue)])

    def _key_pressed(self, _widget, event_key: Gdk.EventKey):
        """If user presses a non-control key, move to search."""
        if Gdk.keyval_to_unicode(event_key.keyval) > 32 or \
                event_key.keyval == Gdk.KEY_BackSpace:
            search_page = self.handlers.get('search_page')
            if not isinstance(search_page, SearchPage):
                return False

            search_page.search_entry.grab_focus_without_selecting()

            if not self.main_notebook:
                return False
            if self.main_notebook.get_current_page() != 0:
                self.main_notebook.set_current_page(0)
            return False

        return False

    def _handle_page_switch(self, _widget, page, _page_num):
        """
        On page switch some things need to happen, mostly cleaning any old
        selections/menu options highlighted.
        """
        page_handler = self.handlers.get(page.get_name())
        if page_handler:
            page_handler.initialize_page()


def main():
    """
    Start the menu app
    """
    # if X is not running or other weird stuff is happening, exit with exit code
    # 6 to signal to the service that this should not be restarted
    if not Gtk.init_check()[0]:
        sys.exit(6)

    qapp = qubesadmin.Qubes()
    dispatcher = qubesadmin.events.EventsDispatcher(qapp)
    app = AppMenu(qapp, dispatcher)
    app.run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
