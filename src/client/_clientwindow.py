import logging
import sys
import time
from functools import partial

from oauthlib.oauth2 import WebApplicationClient
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtNetwork import QNetworkAccessManager
from requests_oauthlib import OAuth2Session

import config
import fa
import notifications as ns
import util
import util.crash
from chat import ChatMVC
from chat._avatarWidget import AvatarWidget
from chat.channel_autojoiner import ChannelAutojoiner
from chat.chat_announcer import ChatAnnouncer
from chat.chat_controller import ChatController
from chat.chat_greeter import ChatGreeter
from chat.chat_view import ChatView
from chat.chatter_model import ChatterLayoutElements
from chat.ircconnection import IrcConnection
from chat.language_channel_config import LanguageChannelConfig
from chat.line_restorer import ChatLineRestorer
from client.aliasviewer import AliasSearchWindow, AliasWindow
from client.chat_config import ChatConfig
from client.clientstate import ClientState
from client.connection import (
    ConnectionState,
    Dispatcher,
    LobbyInfo,
    ServerConnection,
    ServerReconnecter,
)
from client.gameannouncer import GameAnnouncer
from client.login import LoginWidget
from client.playercolors import PlayerColors
from client.theme_menu import ThemeMenu
from client.user import (
    User,
    UserRelationController,
    UserRelationModel,
    UserRelations,
    UserRelationTrackers,
)
from connectivity.ConnectivityDialog import ConnectivityDialog
from coop import CoopWidget
from downloadManager import (
    MAP_PREVIEW_ROOT,
    AvatarDownloader,
    PreviewDownloader,
)
from fa.factions import Factions
from fa.game_runner import GameRunner
from fa.game_session import GameSession
from fa.maps import getUserMapsFolder
from games import GamesWidget
from games.gameitem import GameViewBuilder
from games.gamemodel import GameModel
from games.hostgamewidget import build_launcher
from mapGenerator.mapgenManager import MapGeneratorManager
from model.chat.channel import ChannelID, ChannelType
from model.chat.chat import Chat
from model.chat.chatline import ChatLineMetadataBuilder
from model.gameset import Gameset, PlayerGameIndex
from model.player import Player
from model.playerset import Playerset
from model.rating import MatchmakerQueueType, RatingType
from news import NewsWidget
from power import PowerTools
from replays import ReplaysWidget
from secondaryServer import SecondaryServer
from stats import StatsWidget
from ui.busy_widget import BusyWidget
from ui.status_logo import StatusLogo
from unitdb import unitdbtab
from updater import ClientUpdateTools
from vaults.mapvault.mapvault import MapVault
from vaults.modvault.modvault import ModVault
from vaults.modvault.utils import getModFolder, setModFolder

from .mouse_position import MousePosition
from .oauth_dialog import OAuthWidget

logger = logging.getLogger(__name__)

OAUTH_TOKEN_PATH = "/oauth2/token"
OAUTH_AUTH_PATH = "/oauth2/auth"

FormClass, BaseClass = util.THEME.loadUiType("client/client.ui")


class ClientWindow(FormClass, BaseClass):
    """
    This is the main lobby client that manages the FAF-related connection and
    data, in particular players, games, ranking, etc.
    Its UI also houses all the other UIs for the sub-modules.
    """

    state_changed = QtCore.pyqtSignal(object)
    authorized = QtCore.pyqtSignal(object)

    # These signals notify connected modules of game state changes
    # (i.e. reasons why FA is launched)
    viewing_replay = QtCore.pyqtSignal(object)

    # Game state controls
    game_enter = QtCore.pyqtSignal()
    game_exit = QtCore.pyqtSignal()
    game_full = QtCore.pyqtSignal()

    # These signals propagate important client state changes to other modules
    local_broadcast = QtCore.pyqtSignal(str, str)
    auto_join = QtCore.pyqtSignal(list)
    channels_updated = QtCore.pyqtSignal(list)
    # unofficial_client = QtCore.pyqtSignal(str)

    matchmaker_info = QtCore.pyqtSignal(dict)
    party_invite = QtCore.pyqtSignal(dict)

    remember = config.Settings.persisted_property(
        'user/remember', type=bool, default_value=True,
    )
    refresh_token = config.Settings.persisted_property(
        'user/refreshToken', persist_if=lambda self: self.remember,
    )

    game_logs = config.Settings.persisted_property(
        'game/logs', type=bool, default_value=True,
    )

    use_chat = config.Settings.persisted_property(
        'chat/enabled', type=bool, default_value=True,
    )

    def __init__(self, *args, **kwargs):
        super(ClientWindow, self).__init__(*args, **kwargs)

        logger.debug("Client instantiating")

        # Hook to Qt's application management system
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.cleanup)
        QtWidgets.QApplication.instance().applicationStateChanged.connect(
            self.appStateChanged,
        )

        self._network_access_manager = QNetworkAccessManager(self)
        self.OAuthSession = None
        self.tokenTimer = QtCore.QTimer()
        self.tokenTimer.timeout.connect(self.checkOAuthToken)

        self.unique_id = None
        self._chat_config = ChatConfig(util.settings)

        self.send_file = False
        self.warning_buttons = {}

        # Tray icon
        self.tray = QtWidgets.QSystemTrayIcon()
        self.tray.setIcon(util.THEME.icon("client/tray_icon.png"))
        self.tray.setToolTip("FAF Python Client")
        self.tray.activated.connect(self.handle_tray_icon_activation)
        tray_menu = QtWidgets.QMenu()
        tray_menu.addAction("Open Client", self.show_normal)
        tray_menu.addAction("Quit Client", self.close)
        self.tray.setContextMenu(tray_menu)
        # Mouse down on tray icon deactivates the application.
        # So there is no way to know for sure if the tray icon was clicked from
        # active application or from inactive application. So we assume that
        # if the application was deactivated less than 0.5s ago, then the tray
        # icon click (both left or right button) was made from the active app.
        self._lastDeactivateTime = None
        self.keepActiveForTrayIcon = 0.5
        self.tray.show()

        self._state = ClientState.NONE
        self.session = None
        self.game_session = None

        # This dictates whether we login automatically in the beginning or
        # after a disconnect. We turn it on if we're sure we have correct
        # credentials and want to use them (if we were remembered or after
        # login) and turn it off if we're getting fresh credentials or
        # encounter a serious server error.
        self._auto_relogin = self.remember

        self.lobby_dispatch = Dispatcher()
        self.lobby_connection = ServerConnection(
            config.Settings.get('lobby/host'),
            config.Settings.get('lobby/port', type=int),
            self.lobby_dispatch.dispatch,
        )
        self.lobby_connection.state_changed.connect(
            self.on_connection_state_changed,
        )
        self.lobby_reconnector = ServerReconnecter(self.lobby_connection)

        self.players = Playerset()  # Players known to the client
        self.gameset = Gameset(self.players)
        self._player_game_relation = PlayerGameIndex(
            self.gameset, self.players,
        )

        # FIXME (needed fa/game_process L81 for self.game = self.gameset[uid])
        fa.instance.gameset = self.gameset

        self.lobby_info = LobbyInfo(
            self.lobby_dispatch, self.gameset, self.players,
        )

        # Handy reference to the User object representing the logged-in user.
        self.me = User(self.players)
        self.login = None
        self.id = None

        self._chat_model = Chat.build(
            playerset=self.players,
            base_channels=['#aeolus'],
        )

        relation_model = UserRelationModel.build()
        relation_controller = UserRelationController.build(
            relation_model,
            me=self.me,
            settings=config.Settings,
            lobby_info=self.lobby_info,
            lobby_connection=self.lobby_connection,
        )
        relation_trackers = UserRelationTrackers.build(
            relation_model,
            playerset=self.players,
            chatterset=self._chat_model.chatters,
        )
        self.user_relations = UserRelations(
            relation_model, relation_controller, relation_trackers,
        )
        self.me.relations = self.user_relations

        self.map_downloader = PreviewDownloader(
            util.MAP_PREVIEW_SMALL_DIR,
            util.MAP_PREVIEW_LARGE_DIR,
            MAP_PREVIEW_ROOT,
        )
        self.mod_downloader = PreviewDownloader(
            util.MOD_PREVIEW_DIR, None, None,
        )
        self.avatar_downloader = AvatarDownloader()

        # Map generator
        self.map_generator = MapGeneratorManager()

        # Qt model for displaying active games.
        self.game_model = GameModel(self.me, self.map_downloader, self.gameset)

        self.gameset.added.connect(self.fill_in_session_info)

        self.lobby_info.serverSession.connect(self.handle_session)
        self.lobby_dispatch["registration_response"] = (
            self.handle_registration_response
        )
        self.lobby_dispatch["game_launch"] = self.handle_game_launch
        self.lobby_dispatch["matchmaker_info"] = self.handle_matchmaker_info
        self.lobby_dispatch["player_info"] = self.handle_player_info
        self.lobby_dispatch["notice"] = self.handle_notice
        self.lobby_dispatch["invalid"] = self.handle_invalid
        self.lobby_dispatch["welcome"] = self.handle_welcome
        self.lobby_dispatch["authentication_failed"] = (
            self.handle_authentication_failed
        )
        self.lobby_dispatch["irc_password"] = self.handle_irc_password
        self.lobby_dispatch["update_party"] = self.handle_update_party
        self.lobby_dispatch["kicked_from_party"] = (
            self.handle_kicked_from_party
        )
        self.lobby_dispatch["party_invite"] = self.handle_party_invite
        self.lobby_dispatch["match_found"] = self.handle_match_found_message
        self.lobby_dispatch["match_cancelled"] = self.handle_match_cancelled
        self.lobby_dispatch["search_info"] = self.handle_search_info
        self.lobby_info.social.connect(self.handle_social)

        # Process used to run Forged Alliance (managed in module fa)
        fa.instance.started.connect(self.started_fa)
        fa.instance.finished.connect(self.finished_fa)
        fa.instance.error.connect(self.error_fa)
        self.gameset.added.connect(fa.instance.newServerGame)

        # Local Replay Server
        self.replayServer = fa.replayserver.ReplayServer(self)

        # ConnectivityTest
        self.connectivity = None  # type - ConnectivityHelper

        # stat server
        self.statsServer = SecondaryServer(
            "Statistic", 11002, self.lobby_dispatch,
        )

        # create user interface (main window) and load theme
        self.setupUi(self)
        util.THEME.stylesheets_reloaded.connect(self.load_stylesheet)
        self.load_stylesheet()

        self.setWindowTitle("FA Forever {}".format(util.VERSION_STRING))

        # Frameless
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowSystemMenuHint
            | QtCore.Qt.WindowMinimizeButtonHint,
        )

        self.rubber_band = QtWidgets.QRubberBand(
            QtWidgets.QRubberBand.Rectangle,
        )

        self.mouse_position = MousePosition(self)
        self.installEventFilter(self)  # register events

        self.minimize = QtWidgets.QToolButton(self)
        self.minimize.setIcon(util.THEME.icon("client/minimize-button.png"))

        self.maximize = QtWidgets.QToolButton(self)
        self.maximize.setIcon(util.THEME.icon("client/maximize-button.png"))

        close = QtWidgets.QToolButton(self)
        close.setIcon(util.THEME.icon("client/close-button.png"))

        self.minimize.setMinimumHeight(10)
        close.setMinimumHeight(10)
        self.maximize.setMinimumHeight(10)

        close.setIconSize(QtCore.QSize(22, 22))
        self.minimize.setIconSize(QtCore.QSize(22, 22))
        self.maximize.setIconSize(QtCore.QSize(22, 22))

        close.setProperty("windowControlBtn", True)
        self.maximize.setProperty("windowControlBtn", True)
        self.minimize.setProperty("windowControlBtn", True)

        self.menu = self.menuBar()
        title_label = QtWidgets.QLabel(
            "FA Forever" if not config.is_beta() else "FA Forever BETA",
        )
        title_label.setProperty('titleLabel', True)
        self.topLayout.addWidget(title_label)
        self.topLayout.addStretch(500)
        self.topLayout.addWidget(self.menu)
        self.topLayout.addWidget(self.minimize)
        self.topLayout.addWidget(self.maximize)
        self.topLayout.addWidget(close)
        self.topLayout.setSpacing(0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed,
        )
        self.is_window_maximized = False

        close.clicked.connect(self.close)
        self.minimize.clicked.connect(self.showMinimized)
        self.maximize.clicked.connect(self.show_max_restore)

        self.moving = False
        self.dragging = False
        self.dragging_hover = False
        self.offset = None
        self.current_geometry = None

        self.mainGridLayout.addWidget(QtWidgets.QSizeGrip(self), 2, 2)

        # Wire all important signals
        self._main_tab = -1
        self.mainTabs.currentChanged.connect(self.main_tab_changed)
        self._vault_tab = -1
        self.topTabs.currentChanged.connect(self.vault_tab_changed)

        self.player_colors = PlayerColors(
            self.me, self.user_relations.model, util.THEME,
        )

        self.game_announcer = GameAnnouncer(
            self.gameset, self.me, self.player_colors,
        )

        self.power = 0  # current user power
        self.id = 0
        # Initialize the Menu Bar according to settings etc.
        self._language_channel_config = LanguageChannelConfig(
            self, config.Settings, util.THEME,
        )
        self.initMenus()

        # Load the icons for the tabs
        self.mainTabs.setTabIcon(
            self.mainTabs.indexOf(self.whatNewTab),
            util.THEME.icon("client/feed.png"),
        )
        self.mainTabs.setTabIcon(
            self.mainTabs.indexOf(self.chatTab),
            util.THEME.icon("client/chat.png"),
        )
        self.mainTabs.setTabIcon(
            self.mainTabs.indexOf(self.gamesTab),
            util.THEME.icon("client/games.png"),
        )
        self.mainTabs.setTabIcon(
            self.mainTabs.indexOf(self.coopTab),
            util.THEME.icon("client/coop.png"),
        )
        self.mainTabs.setTabIcon(
            self.mainTabs.indexOf(self.vaultsTab),
            util.THEME.icon("client/mods.png"),
        )
        self.mainTabs.setTabIcon(
            self.mainTabs.indexOf(self.ladderTab),
            util.THEME.icon("client/ladder.png"),
        )
        self.mainTabs.setTabIcon(
            self.mainTabs.indexOf(self.tourneyTab),
            util.THEME.icon("client/tourney.png"),
        )
        self.mainTabs.setTabIcon(
            self.mainTabs.indexOf(self.unitdbTab),
            util.THEME.icon("client/unitdb.png"),
        )
        self.mainTabs.setTabIcon(
            self.mainTabs.indexOf(self.replaysTab),
            util.THEME.icon("client/replays.png"),
        )
        self.mainTabs.setTabIcon(
            self.mainTabs.indexOf(self.tutorialsTab),
            util.THEME.icon("client/tutorials.png"),
        )

        # for moderator
        self.mod_menu = None
        self.power_tools = PowerTools.build(
            playerset=self.players,
            lobby_connection=self.lobby_connection,
            theme=util.THEME,
            parent_widget=self,
            settings=config.Settings,
        )

        self._alias_viewer = AliasWindow.build(parent_widget=self)
        self._alias_search_window = AliasSearchWindow(self, self._alias_viewer)
        self._game_runner = GameRunner(self.gameset, self)

        self.connectivity_dialog = None

    def load_stylesheet(self):
        self.setStyleSheet(util.THEME.readstylesheet("client/client.css"))

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        self._state = value
        self.state_changed.emit(value)

    def on_connection_state_changed(self, state):
        if self.state == ClientState.SHUTDOWN:
            return

        if state == ConnectionState.CONNECTED:
            self.on_connected()
            self.state = ClientState.CONNECTED
        elif state == ConnectionState.DISCONNECTED:
            self.on_disconnected()
            self.state = ClientState.DISCONNECTED
        elif state == ConnectionState.CONNECTING:
            self.state = ClientState.CONNECTING

    def on_connected(self):
        # Enable reconnect in case we used to explicitly stay offline
        self.lobby_reconnector.enabled = True
        self.lobby_connection.send(
            dict(
                command="ask_session",
                version=config.VERSION,
                user_agent="faf-client",
            ),
        )

    def on_disconnected(self):
        logger.warning("Disconnected from lobby server.")
        self.gameset.clear()
        self.clear_players()
        self.games.stopSearch()

    def appStateChanged(self, state):
        if state == QtCore.Qt.ApplicationInactive:
            self._lastDeactivateTime = time.time()

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.HoverMove:
            self.dragging_hover = self.dragging
            if self.dragging:
                self.resize_widget(self.mapToGlobal(event.pos()))
            else:
                if not self.is_window_maximized:
                    self.mouse_position.update_mouse_position(event.pos())
                else:
                    self.mouse_position.reset_to_false()
            self.update_cursor_shape()

        return False

    def update_cursor_shape(self):
        if (
            self.mouse_position.on_top_left_edge
            or self.mouse_position.on_bottom_right_edge
        ):
            self.mouse_position.cursor_shape_change = True
            self.setCursor(QtCore.Qt.SizeFDiagCursor)
        elif (
            self.mouse_position.on_top_right_edge
            or self.mouse_position.on_bottom_left_edge
        ):
            self.setCursor(QtCore.Qt.SizeBDiagCursor)
            self.mouse_position.cursor_shape_change = True
        elif (
            self.mouse_position.on_left_edge
            or self.mouse_position.on_right_edge
        ):
            self.setCursor(QtCore.Qt.SizeHorCursor)
            self.mouse_position.cursor_shape_change = True
        elif (
            self.mouse_position.on_top_edge
            or self.mouse_position.on_bottom_edge
        ):
            self.setCursor(QtCore.Qt.SizeVerCursor)
            self.mouse_position.cursor_shape_change = True
        else:
            if self.mouse_position.cursor_shape_change:
                self.unsetCursor()
                self.mouse_position.cursor_shape_change = False

    def handle_tray_icon_activation(self, reason):
        if reason == QtWidgets.QSystemTrayIcon.Trigger:
            inactiveTime = time.time() - self._lastDeactivateTime
            if (
                self.isMinimized()
                or inactiveTime >= self.keepActiveForTrayIcon
            ):
                self.show_normal()
            else:
                self.showMinimized()
        elif reason == QtWidgets.QSystemTrayIcon.Context:
            position = QtGui.QCursor.pos()
            position.setY(position.y() - self.tray.contextMenu().height())
            self.tray.contextMenu().popup(position)

    def show_normal(self):
        self.showNormal()
        self.activateWindow()

    def show_max_restore(self):
        if self.is_window_maximized:
            self.is_window_maximized = False
            if self.current_geometry:
                self.setGeometry(self.current_geometry)

        else:
            self.is_window_maximized = True
            self.current_geometry = self.geometry()
            self.setGeometry(
                QtWidgets.QDesktopWidget().availableGeometry(self),
            )

    def mouseDoubleClickEvent(self, event):
        self.show_max_restore()

    def mouseReleaseEvent(self, event):
        self.dragging = False
        self.moving = False
        if self.rubber_band.isVisible():
            self.is_window_maximized = True
            self.current_geometry = self.geometry()
            self.setGeometry(self.rubber_band.geometry())
            self.rubber_band.hide()
            # self.show_max_restore()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            if (
                self.mouse_position.is_on_edge()
                and not self.is_window_maximized
            ):
                self.dragging = True
                return
            else:
                self.dragging = False

            self.moving = True
            self.offset = event.pos()

    def mouseMoveEvent(self, event):
        if self.dragging and not self.dragging_hover:
            self.resize_widget(event.globalPos())

        elif self.moving and self.offset is not None:
            desktop = QtWidgets.QDesktopWidget().availableGeometry(self)
            if event.globalPos().y() == 0:
                self.rubber_band.setGeometry(desktop)
                self.rubber_band.show()
            elif event.globalPos().x() == 0:
                desktop.setRight(desktop.right() / 2.0)
                self.rubber_band.setGeometry(desktop)
                self.rubber_band.show()
            elif event.globalPos().x() == desktop.right():
                desktop.setRight(desktop.right() / 2.0)
                desktop.moveLeft(desktop.right())
                self.rubber_band.setGeometry(desktop)
                self.rubber_band.show()

            else:
                self.rubber_band.hide()
                if self.is_window_maximized:
                    self.show_max_restore()

            self.move(event.globalPos() - self.offset)

    def resize_widget(self, mouse_position):
        if mouse_position.y() == 0:
            self.rubber_band.setGeometry(
                QtWidgets.QDesktopWidget().availableGeometry(self),
            )
            self.rubber_band.show()
        else:
            self.rubber_band.hide()

        orig_rect = self.frameGeometry()

        left, top, right, bottom = orig_rect.getCoords()
        min_width = self.minimumWidth()
        min_height = self.minimumHeight()
        if self.mouse_position.on_top_left_edge:
            left = mouse_position.x()
            top = mouse_position.y()

        elif self.mouse_position.on_bottom_left_edge:
            left = mouse_position.x()
            bottom = mouse_position.y()
        elif self.mouse_position.on_top_right_edge:
            right = mouse_position.x()
            top = mouse_position.y()
        elif self.mouse_position.on_bottom_right_edge:
            right = mouse_position.x()
            bottom = mouse_position.y()
        elif self.mouse_position.on_left_edge:
            left = mouse_position.x()
        elif self.mouse_position.on_right_edge:
            right = mouse_position.x()
        elif self.mouse_position.on_top_edge:
            top = mouse_position.y()
        elif self.mouse_position.on_bottom_edge:
            bottom = mouse_position.y()

        new_rect = QtCore.QRect(
            QtCore.QPoint(left, top),
            QtCore.QPoint(right, bottom),
        )
        if new_rect.isValid():
            if min_width > new_rect.width():
                if left != orig_rect.left():
                    new_rect.setLeft(orig_rect.left())
                else:
                    new_rect.setRight(orig_rect.right())
            if min_height > new_rect.height():
                if top != orig_rect.top():
                    new_rect.setTop(orig_rect.top())
                else:
                    new_rect.setBottom(orig_rect.bottom())

            self.setGeometry(new_rect)

    def setup(self):
        self.load_settings()
        self._chat_config.channel_blink_interval = 500
        self._chat_config.channel_ping_timeout = 60 * 1000
        self._chat_config.max_chat_lines = 200
        self._chat_config.chat_line_trim_count = 50
        self._chat_config.announcement_channels = ['#aeolus']
        self._chat_config.channels_to_greet_in = ['#aeolus']
        self._chat_config.newbie_channel_game_threshold = 50

        wiki_link = util.Settings.get("WIKI_URL")
        wiki_formatter = "Check out the wiki: {} for help with common issues."
        wiki_msg = wiki_formatter.format(wiki_link)

        self._chat_config.channel_greeting = [
            ("Welcome to Forged Alliance Forever!", "red", "+3"),
            (wiki_msg, "white", "+1"),
            ("", "black", "+1"),
            ("", "black", "+1"),
        ]

        self.gameview_builder = GameViewBuilder(self.me, self.player_colors)
        self.game_launcher = build_launcher(
            self.players, self.me,
            self, self.gameview_builder,
            self.map_downloader,
        )
        self._avatar_widget_builder = AvatarWidget.builder(
            parent_widget=self,
            lobby_connection=self.lobby_connection,
            lobby_info=self.lobby_info,
            avatar_dler=self.avatar_downloader,
            theme=util.THEME,
        )

        chat_connection = IrcConnection.build(settings=config.Settings)
        line_metadata_builder = ChatLineMetadataBuilder.build(
            me=self.me,
            user_relations=self.user_relations.model,
        )

        chat_controller = ChatController.build(
            connection=chat_connection,
            model=self._chat_model,
            user_relations=self.user_relations.model,
            chat_config=self._chat_config,
            me=self.me,
            line_metadata_builder=line_metadata_builder,
        )

        target_channel = ChannelID(ChannelType.PUBLIC, '#aeolus')
        chat_view = ChatView.build(
            target_viewed_channel=target_channel,
            model=self._chat_model,
            controller=chat_controller,
            parent_widget=self,
            theme=util.THEME,
            chat_config=self._chat_config,
            player_colors=self.player_colors,
            me=self.me,
            user_relations=self.user_relations,
            power_tools=self.power_tools,
            map_preview_dler=self.map_downloader,
            avatar_dler=self.avatar_downloader,
            avatar_widget_builder=self._avatar_widget_builder,
            alias_viewer=self._alias_viewer,
            client_window=self,
            game_runner=self._game_runner,
        )

        channel_autojoiner = ChannelAutojoiner.build(
            base_channels=['#aeolus'],
            model=self._chat_model,
            controller=chat_controller,
            settings=config.Settings,
            lobby_info=self.lobby_info,
            chat_config=self._chat_config,
            me=self.me,
        )
        chat_greeter = ChatGreeter(
            model=self._chat_model,
            theme=util.THEME,
            chat_config=self._chat_config,
            line_metadata_builder=line_metadata_builder,
        )
        chat_restorer = ChatLineRestorer(self._chat_model)
        chat_announcer = ChatAnnouncer(
            model=self._chat_model,
            chat_config=self._chat_config,
            game_announcer=self.game_announcer,
            line_metadata_builder=line_metadata_builder,
        )

        self._chatMVC = ChatMVC(
            self._chat_model, line_metadata_builder,
            chat_connection, chat_controller,
            channel_autojoiner, chat_greeter,
            chat_restorer, chat_announcer, chat_view,
        )

        self.authorized.connect(self._connect_chat)

        self.logo = StatusLogo(self, self._chatMVC.model)
        self.logo.disconnect_requested.connect(self.disconnect_)
        self.logo.reconnect_requested.connect(self.reconnect)
        self.logo.chat_reconnect_requested.connect(self.chat_reconnect)
        self.logo.about_dialog_requested.connect(self.linkAbout)
        self.logo.connectivity_dialog_requested.connect(
            self.connectivityDialog,
        )
        self.topLayout.insertWidget(0, self.logo)

        # build main window with the now active client
        self.news = NewsWidget(self)
        self.coop = CoopWidget(
            self, self.game_model, self.me,
            self.gameview_builder, self.game_launcher,
        )
        self.games = GamesWidget(
            self, self.game_model, self.me,
            self.gameview_builder, self.game_launcher,
        )
        self.ladder = StatsWidget(self)
        self.replays = ReplaysWidget(
            self, self.lobby_dispatch, self.gameset, self.players,
        )
        self.mapvault = MapVault(self)
        self.modvault = ModVault(self)
        self.notificationSystem = ns.Notifications(
            self, self.gameset, self.players, self.me,
        )

        self._unitdb = unitdbtab.build_db_tab(config.UNITDB_CONFIG_FILE)

        # TODO: some day when the tabs only do UI we'll have all this in the
        # .ui file
        self.whatNewTab.layout().addWidget(self.news)
        self.chatTab.layout().addWidget(self._chatMVC.view.widget.base)
        self.coopTab.layout().addWidget(self.coop)
        self.gamesTab.layout().addWidget(self.games)
        self.ladderTab.layout().addWidget(self.ladder)
        self.replaysTab.layout().addWidget(self.replays)
        self.mapsTab.layout().addWidget(self.mapvault)
        self.unitdbTab.layout().addWidget(self._unitdb.db_widget)
        self.modsTab.layout().addWidget(self.modvault)

        # TODO: hiding some non-functional tabs. Either prune them or implement
        # something useful in them.
        self.mainTabs.removeTab(self.mainTabs.indexOf(self.tutorialsTab))
        self.mainTabs.removeTab(self.mainTabs.indexOf(self.tourneyTab))

        self.mainTabs.setCurrentIndex(self.mainTabs.indexOf(self.whatNewTab))

        # set menu states
        self.actionNsEnabled.setChecked(
            self.notificationSystem.settings.enabled,
        )

        # warning setup
        self.labelAutomatchInfo.hide()
        self.warning = QtWidgets.QHBoxLayout()

        self.warnPlayer = QtWidgets.QLabel(self)
        self.warnPlayer.setText(
            "A player of your skill level is currently searching for a 1v1 "
            "game. Click a faction to join them! ",
        )
        self.warnPlayer.setAlignment(QtCore.Qt.AlignHCenter)
        self.warnPlayer.setAlignment(QtCore.Qt.AlignVCenter)
        self.warnPlayer.setProperty("warning", True)
        self.warning.addStretch()
        self.warning.addWidget(self.warnPlayer)

        def add_warning_button(faction):
            button = QtWidgets.QToolButton(self)
            button.setMaximumSize(25, 25)
            button.setIcon(
                util.THEME.icon(
                    "games/automatch/{}.png".format(faction.to_name()),
                ),
            )
            button.clicked.connect(partial(self.ladderWarningClicked, faction))
            self.warning.addWidget(button)
            return button

        self.warning_buttons = {
            faction: add_warning_button(faction)
            for faction in Factions
        }

        self.warning.addStretch()

        self.mainGridLayout.addLayout(self.warning, 2, 0)
        self.warningHide()

        self._update_tools = ClientUpdateTools.build(
            config.VERSION, self, self._network_access_manager,
        )
        self._update_tools.mandatory_update_aborted.connect(self.close)
        self._update_tools.checker.check()

    def _connect_chat(self, me):
        if not self.use_chat:
            return
        self._chatMVC.connection.connect_(me.login, me.id, self.irc_password)

    def warningHide(self):
        """
        hide the warning bar for matchmaker
        """
        self.warnPlayer.hide()
        for i in list(self.warning_buttons.values()):
            i.hide()

    def warningShow(self):
        """
        show the warning bar for matchmaker
        """
        self.warnPlayer.show()
        for i in list(self.warning_buttons.values()):
            i.show()

    def reconnect(self):
        self.lobby_reconnector.enabled = True
        self.try_to_auto_login()

    def disconnect_(self):
        if self.state != ClientState.DISCONNECTED:
            # Used when the user explicitly demanded to stay offline.
            self._auto_relogin = self.remember
            self.lobby_reconnector.enabled = False
            self.lobby_connection.disconnect_()
            self._chatMVC.connection.disconnect_()
            self.games.onLogOut()
            self.tokenTimer.stop()
            config.Settings.set("oauth/token", None, persist=False)

    def chat_reconnect(self):
        self._connect_chat(self.me)

    @QtCore.pyqtSlot()
    def cleanup(self):
        """
        Perform cleanup before the UI closes
        """
        self.state = ClientState.SHUTDOWN

        progress = QtWidgets.QProgressDialog()
        progress.setMinimum(0)
        progress.setMaximum(0)
        progress.setWindowTitle("FAF is shutting down")
        progress.setMinimum(0)
        progress.setMaximum(0)
        progress.setValue(0)
        progress.setCancelButton(None)
        progress.show()

        # Important: If a game is running, offer to terminate it gently
        progress.setLabelText("Closing ForgedAllianceForever.exe")
        if fa.instance.running():
            fa.instance.close()

        # Terminate Lobby Server connection
        self.lobby_reconnector.enabled = False
        if self.lobby_connection.socket_connected():
            progress.setLabelText("Closing main connection.")
            self.lobby_connection.disconnect_()

        # Close connectivity dialog
        if self.connectivity_dialog is not None:
            self.connectivity_dialog.close()
            self.connectivity_dialog = None
        # Close game session (and stop faf-ice-adapter.exe)
        if self.game_session is not None:
            self.game_session.closeIceAdapter()
            self.game_session = None

        # Terminate local ReplayServer
        if self.replayServer:
            progress.setLabelText("Terminating local replay server")
            self.replayServer.close()
            self.replayServer = None

        # Clean up Chat
        if self._chatMVC:
            progress.setLabelText("Disconnecting from IRC")
            self._chatMVC.connection.disconnect_()
            self._chatMVC = None

        # Clear cached game files if needed
        util.clearGameCache()

        # Get rid of generated maps
        util.clearGeneratedMaps()

        # Get rid of the Tray icon
        if self.tray:
            progress.setLabelText("Removing System Tray icon")
            self.tray.deleteLater()
            self.tray = None

        # Clear qt message handler to avoid crash at exit
        config.clear_logging_handlers()

        # Terminate UI
        if self.isVisible():
            progress.setLabelText("Closing main window")
            self.close()

        progress.close()

    def closeEvent(self, event):
        logger.info("Close Event for Application Main Window")
        self.saveWindow()

        if fa.instance.running():
            result = QtWidgets.QMessageBox.question(
                self,
                "Are you sure?",
                (
                    "Seems like you still have Forged Alliance running!"
                    "<br/><b>Close anyway?</b>"
                ),
                QtWidgets.QMessageBox.Yes,
                QtWidgets.QMessageBox.No,
            )
            if result == QtWidgets.QMessageBox.No:
                event.ignore()
                return

        return QtWidgets.QMainWindow.closeEvent(self, event)

    def initMenus(self):
        self.actionCheck_for_Updates.triggered.connect(self.check_for_updates)
        self.actionUpdate_Settings.triggered.connect(self.show_update_settings)
        self.actionLink_account_to_Steam.triggered.connect(
            partial(self.open_url, config.Settings.get("STEAMLINK_URL")),
        )
        self.actionLinkWebsite.triggered.connect(
            partial(self.open_url, config.Settings.get("WEBSITE_URL")),
        )
        self.actionLinkWiki.triggered.connect(
            partial(self.open_url, config.Settings.get("WIKI_URL")),
        )
        self.actionLinkForums.triggered.connect(
            partial(self.open_url, config.Settings.get("FORUMS_URL")),
        )
        self.actionLinkUnitDB.triggered.connect(
            partial(self.open_url, config.Settings.get("UNITDB_URL")),
        )
        self.actionLinkMapPool.triggered.connect(
            partial(self.open_url, config.Settings.get("MAPPOOL_URL")),
        )
        self.actionLinkGitHub.triggered.connect(
            partial(self.open_url, config.Settings.get("GITHUB_URL")),
        )

        self.actionNsSettings.triggered.connect(
            lambda: self.notificationSystem.on_showSettings(),
        )
        self.actionNsEnabled.triggered.connect(
            lambda enabled: self.notificationSystem.setNotificationEnabled(
                enabled,
            ),
        )

        self.actionWiki.triggered.connect(
            partial(self.open_url, config.Settings.get("WIKI_URL")),
        )
        self.actionReportBug.triggered.connect(
            partial(self.open_url, config.Settings.get("TICKET_URL")),
        )
        self.actionShowLogs.triggered.connect(self.linkShowLogs)
        self.actionTechSupport.triggered.connect(
            partial(self.open_url, config.Settings.get("SUPPORT_URL")),
        )
        self.actionAbout.triggered.connect(self.linkAbout)

        self.actionClearCache.triggered.connect(self.clearCache)
        self.actionClearSettings.triggered.connect(self.clearSettings)
        self.actionClearGameFiles.triggered.connect(self.clearGameFiles)
        self.actionClearMapGenerators.triggered.connect(
            self.clearMapGenerators,
        )

        self.actionSetGamePath.triggered.connect(self.switchPath)

        self.actionShowMapsDir.triggered.connect(
            lambda: util.showDirInFileBrowser(getUserMapsFolder()),
        )
        self.actionShowModsDir.triggered.connect(
            lambda: util.showDirInFileBrowser(getModFolder()),
        )
        self.actionShowReplaysDir.triggered.connect(
            lambda: util.showDirInFileBrowser(util.REPLAY_DIR),
        )
        self.actionShowThemesDir.triggered.connect(
            lambda: util.showDirInFileBrowser(util.THEME_DIR),
        )
        self.actionShowGamePrefs.triggered.connect(
            lambda: util.showDirInFileBrowser(util.LOCALFOLDER),
        )
        self.actionShowClientConfigFile.triggered.connect(util.showConfigFile)

        # Toggle-Options
        self.actionSetAutoLogin.triggered.connect(self.update_options)
        self.actionSetAutoLogin.setChecked(self.remember)
        self.actionSetAutoDownloadMods.toggled.connect(
            self.on_action_auto_download_mods_toggled,
        )
        self.actionSetAutoDownloadMods.setChecked(
            config.Settings.get('mods/autodownload', type=bool, default=False),
        )
        self.actionSetAutoDownloadMaps.toggled.connect(
            self.on_action_auto_download_maps_toggled,
        )
        self.actionSetAutoDownloadMaps.setChecked(
            config.Settings.get('maps/autodownload', type=bool, default=False),
        )
        self.actionSetAutoGenerateMaps.toggled.connect(
            self.on_action_auto_generate_maps_toggled,
        )
        self.actionSetAutoGenerateMaps.setChecked(
            config.Settings.get(
                'mapGenerator/autostart',
                type=bool,
                default=False,
            ),
        )
        self.actionSetSoundEffects.triggered.connect(self.update_options)
        self.actionSetOpenGames.triggered.connect(self.update_options)
        self.actionSetJoinsParts.triggered.connect(self.update_options)
        self.actionSetNewbiesChannel.triggered.connect(self.update_options)
        self.actionIgnoreFoes.triggered.connect(self.update_options)
        self.actionSetLiveReplays.triggered.connect(self.update_options)
        self.actionSaveGamelogs.setChecked(self.game_logs)
        self.actionColoredNicknames.triggered.connect(self.update_options)
        self.actionFriendsOnTop.triggered.connect(self.update_options)
        self.actionSetAutoJoinChannels.triggered.connect(
            self.show_autojoin_settings_dialog,
        )
        self.actionSaveGamelogs.toggled.connect(
            self.on_action_save_game_logs_toggled,
        )
        self.actionVaultFallback.toggled.connect(
            self.on_action_fault_fallback_toggled,
        )
        self.actionVaultFallback.setChecked(
            config.Settings.get('vault/fallback', type=bool, default=False),
        )
        self.actionLanguageChannels.triggered.connect(
            self._language_channel_config.run,
        )

        self.actionEnableIceAdapterInfoWindow.triggered.connect(
            self.on_action_enable_ice_adapter_info_window,
        )
        self.actionEnableIceAdapterInfoWindow.setChecked(
            config.Settings.get(
                'iceadapter/info_window',
                type=bool,
                default=False,
            ),
        )
        self.actionSetIceAdapterWindowLaunchDelay.triggered.connect(
            self.set_ice_adapter_window_launch_delay,
        )

        self.actionDoNotKeep.setChecked(
            config.Settings.get('cache/do_not_keep', type=bool, default=True),
        )
        self.actionForever.setChecked(
            config.Settings.get('cache/forever', type=bool, default=False),
        )
        self.actionSetYourOwnTimeInterval.setChecked(
            config.Settings.get(
                'cache/own_settings', type=bool, default=False,
            ),
        )
        self.actionKeepCacheWhileInSession.setChecked(
            config.Settings.get('cache/in_session', type=bool, default=False),
        )
        self.actionKeepCacheWhileInSession.setVisible(
            config.Settings.get('cache/do_not_keep', type=bool, default=True),
        )
        self.actionDoNotKeep.triggered.connect(self.saveCacheSettings)
        self.actionForever.triggered.connect(
            lambda: self.saveCacheSettings(own=False, forever=True),
        )
        self.actionSetYourOwnTimeInterval.triggered.connect(
            lambda: self.saveCacheSettings(own=True, forever=False),
        )
        self.actionKeepCacheWhileInSession.toggled.connect(self.inSessionCache)

        self.actionCheckPlayerAliases.triggered.connect(
            self.checkPlayerAliases,
        )

        self._menuThemeHandler = ThemeMenu(self.menuTheme)
        self._menuThemeHandler.setup(util.THEME.listThemes())
        self._menuThemeHandler.themeSelected.connect(
            lambda theme: util.THEME.setTheme(theme, True),
        )

        self._chat_vis_actions = {
            ChatterLayoutElements.RANK: self.actionHideChatterRank,
            ChatterLayoutElements.AVATAR: self.actionHideChatterAvatar,
            ChatterLayoutElements.COUNTRY: self.actionHideChatterCountry,
            ChatterLayoutElements.NICK: self.actionHideChatterNick,
            ChatterLayoutElements.STATUS: self.actionHideChatterStatus,
            ChatterLayoutElements.MAP: self.actionHideChatterMap,
        }
        for action in self._chat_vis_actions.values():
            action.triggered.connect(self.update_options)

    @QtCore.pyqtSlot()
    def update_options(self):
        chat_config = self._chat_config

        self.remember = self.actionSetAutoLogin.isChecked()
        if self.remember and self.refresh_token:
            config.Settings.set('user/refreshToken', self.refresh_token)
        chat_config.soundeffects = self.actionSetSoundEffects.isChecked()
        chat_config.joinsparts = self.actionSetJoinsParts.isChecked()
        chat_config.newbies_channel = self.actionSetNewbiesChannel.isChecked()
        chat_config.ignore_foes = self.actionIgnoreFoes.isChecked()
        chat_config.friendsontop = self.actionFriendsOnTop.isChecked()

        invisible_items = [
            i for i, a in self._chat_vis_actions.items() if a.isChecked()
        ]
        chat_config.hide_chatter_items.clear()
        chat_config.hide_chatter_items |= invisible_items

        announce_games = self.actionSetOpenGames.isChecked()
        self.game_announcer.announce_games = announce_games
        announce_replays = self.actionSetLiveReplays.isChecked()
        self.game_announcer.announce_replays = announce_replays

        self.game_logs = self.actionSaveGamelogs.isChecked()
        colored_nicknames = self.actionColoredNicknames.isChecked()
        self.player_colors.colored_nicknames = colored_nicknames

        self.saveChat()

    @QtCore.pyqtSlot(bool)
    def on_action_save_game_logs_toggled(self, value):
        self.game_logs = value

    @QtCore.pyqtSlot(bool)
    def on_action_auto_download_mods_toggled(self, value):
        config.Settings.set('mods/autodownload', value is True)

    @QtCore.pyqtSlot(bool)
    def on_action_auto_download_maps_toggled(self, value):
        config.Settings.set('maps/autodownload', value is True)

    @QtCore.pyqtSlot(bool)
    def on_action_auto_generate_maps_toggled(self, value):
        config.Settings.set('mapGenerator/autostart', value is True)

    @QtCore.pyqtSlot(bool)
    def on_action_fault_fallback_toggled(self, value):
        config.Settings.set('vault/fallback', value is True)
        util.setPersonalDir()
        setModFolder()

    @QtCore.pyqtSlot(bool)
    def on_action_enable_ice_adapter_info_window(self, value):
        config.Settings.set('iceadapter/info_window', value is True)

    @QtCore.pyqtSlot()
    def set_ice_adapter_window_launch_delay(self):
        seconds, ok = QtWidgets.QInputDialog().getInt(
            self,
            'Set time interval',
            'Delay the launch of the info window by seconds:',
            config.Settings.get(
                'iceadapter/delay_ui_seconds', type=int, default=10,
            ),
            min=0,
            max=2147483647,
            step=1,
        )
        if ok and seconds:
            config.Settings.set('iceadapter/delay_ui_seconds', seconds)

    @QtCore.pyqtSlot()
    def switchPath(self):
        fa.wizards.Wizard(self).exec_()

    @QtCore.pyqtSlot()
    def clearSettings(self):
        result = QtWidgets.QMessageBox.question(
            self,
            "Clear Settings",
            "Are you sure you wish to clear all settings, "
            "login info, etc. used by this program?",
            QtWidgets.QMessageBox.Yes,
            QtWidgets.QMessageBox.No,
        )
        if result == QtWidgets.QMessageBox.Yes:
            util.settings.clear()
            util.settings.sync()
            QtWidgets.QMessageBox.information(
                self, "Restart Needed", "FAF will quit now.",
            )
            QtWidgets.QApplication.quit()

    @QtCore.pyqtSlot()
    def clearGameFiles(self):
        util.clearDirectory(util.BIN_DIR)
        util.clearDirectory(util.GAMEDATA_DIR)

    @QtCore.pyqtSlot()
    def clearCache(self):
        changed = util.clearDirectory(util.CACHE_DIR)
        if changed:
            QtWidgets.QMessageBox.information(
                self, "Restart Needed", "FAF will quit now.",
            )
            QtWidgets.QApplication.quit()

    @QtCore.pyqtSlot()
    def clearMapGenerators(self):
        util.clearDirectory(util.MAPGEN_DIR)

    # Clear the online users lists
    def clear_players(self):
        self.players.clear()

    @QtCore.pyqtSlot(str)
    def open_url(self, url):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    @QtCore.pyqtSlot()
    def linkShowLogs(self):
        util.showDirInFileBrowser(util.LOG_DIR)

    @QtCore.pyqtSlot()
    def connectivityDialog(self):
        if (
            self.game_session is not None
            and self.game_session.ice_adapter_client is not None
        ):
            self.connectivity_dialog = ConnectivityDialog(
                self.game_session.ice_adapter_client,
            )
            self.connectivity_dialog.show()
        else:
            QtWidgets.QMessageBox().information(
                self,
                "No game",
                "The connectivity window is only available during the game.",
            )

    @QtCore.pyqtSlot()
    def linkAbout(self):
        dialog = util.THEME.loadUi("client/about.ui")
        dialog.version_label.setText("Version: {}".format(util.VERSION_STRING))
        dialog.exec_()

    @QtCore.pyqtSlot()
    def check_for_updates(self):
        self._update_tools.checker.check(always_notify=True)

    @QtCore.pyqtSlot()
    def show_update_settings(self):
        dialog = self._update_tools.settings_dialog()
        dialog.show()

    def checkPlayerAliases(self):
        self._alias_search_window.run()

    def saveWindow(self):
        util.settings.beginGroup("window")
        util.settings.setValue("geometry", self.saveGeometry())
        util.settings.endGroup()

    def show_autojoin_settings_dialog(self):
        autojoin_channels_list = config.Settings.get(
            'chat/auto_join_channels',
            default=[],
        )
        text_of_autojoin_settings_dialog = """
        Enter the list of channels you want to autojoin at startup, separated
        by ; For example: #poker;#newbie To disable autojoining channels,
        leave the box empty and press OK.
        """
        channels_input_of_user, ok = QtWidgets.QInputDialog.getText(
            self,
            'Set autojoin channels',
            text_of_autojoin_settings_dialog,
            QtWidgets.QLineEdit.Normal,
            ';'.join(autojoin_channels_list),
        )
        if ok:
            channels = [
                c.strip()
                for c in channels_input_of_user.split(';')
                if c
            ]
            config.Settings.set('chat/auto_join_channels', channels)

    @QtCore.pyqtSlot(bool)
    def inSessionCache(self, value):
        config.Settings.set('cache/in_session', value is True)

    @QtCore.pyqtSlot()
    def saveCacheSettings(self, own=False, forever=False):
        if forever:
            util.settings.beginGroup('cache')
            util.settings.setValue('do_not_keep', False)
            util.settings.setValue('forever', True)
            util.settings.setValue('own_settings', False)
            util.settings.setValue('number_of_days', -1)
            util.settings.endGroup()
            self.actionKeepCacheWhileInSession.setChecked(False)
        elif own:
            days, ok = QtWidgets.QInputDialog().getInt(
                self,
                'Set time interval',
                'Keep game files in cache for this number of days:',
                config.Settings.get(
                    'cache/number_of_days', type=int, default=30,
                ),
                min=1,
                max=2147483647,
                step=10,
            )
            if ok and days:
                util.settings.beginGroup('cache')
                util.settings.setValue('do_not_keep', False)
                util.settings.setValue('forever', False)
                util.settings.setValue('own_settings', True)
                util.settings.setValue('number_of_days', days)
                util.settings.endGroup()
                self.actionKeepCacheWhileInSession.setChecked(False)
        else:
            util.settings.beginGroup('cache')
            util.settings.setValue('do_not_keep', True)
            util.settings.setValue('forever', False)
            util.settings.setValue('own_settings', False)
            util.settings.setValue('number_of_days', 0)
            util.settings.endGroup()
        self.actionDoNotKeep.setChecked(
            config.Settings.get('cache/do_not_keep', type=bool, default=True),
        )
        self.actionForever.setChecked(
            config.Settings.get('cache/forever', type=bool, default=False),
        )
        self.actionSetYourOwnTimeInterval.setChecked(
            config.Settings.get(
                'cache/own_settings', type=bool, default=False,
            ),
        )
        self.actionKeepCacheWhileInSession.setVisible(
            config.Settings.get('cache/do_not_keep', type=bool, default=True),
        )

    def saveChat(self):
        util.settings.beginGroup("chat")
        util.settings.setValue(
            "livereplays", self.game_announcer.announce_replays,
        )
        util.settings.setValue("opengames", self.game_announcer.announce_games)
        util.settings.setValue(
            "coloredNicknames", self.player_colors.colored_nicknames,
        )
        util.settings.endGroup()
        self._chat_config.save_settings()

    def load_settings(self):
        self.load_chat()
        # Load settings
        util.settings.beginGroup("window")
        geometry = util.settings.value("geometry", None)
        if geometry:
            self.restoreGeometry(geometry)
        util.settings.endGroup()

        util.settings.beginGroup("ForgedAlliance")
        util.settings.endGroup()

    def load_chat(self):
        cc = self._chat_config
        try:
            util.settings.beginGroup("chat")
            self.game_announcer.announce_games = (
                util.settings.value("opengames", "true") == "true"
            )
            self.game_announcer.announce_replays = (
                util.settings.value("livereplays", "true") == "true"
            )
            self.player_colors.colored_nicknames = (
                util.settings.value("coloredNicknames", "false") == "true"
            )
            util.settings.endGroup()
            cc.load_settings()
            self.actionColoredNicknames.setChecked(
                self.player_colors.colored_nicknames,
            )
            self.actionFriendsOnTop.setChecked(cc.friendsontop)

            for item in ChatterLayoutElements:
                self._chat_vis_actions[item].setChecked(
                    item in cc.hide_chatter_items,
                )
            self.actionSetSoundEffects.setChecked(cc.soundeffects)
            self.actionSetLiveReplays.setChecked(
                self.game_announcer.announce_replays,
            )
            self.actionSetOpenGames.setChecked(
                self.game_announcer.announce_games,
            )
            self.actionSetJoinsParts.setChecked(cc.joinsparts)
            self.actionSetNewbiesChannel.setChecked(cc.newbies_channel)
            self.actionIgnoreFoes.setChecked(cc.ignore_foes)
        except BaseException:
            pass

    def do_connect(self):
        if not self.replayServer.doListen():
            return False

        self.lobby_connection.do_connect()
        return True

    def set_remember(self, remember):
        self.remember = remember
        # FIXME - option updating is silly
        self.actionSetAutoLogin.setChecked(self.remember)

    def try_to_auto_login(self):
        if (
            self._auto_relogin
            and self.refresh_token
            and self.refreshOAuthToken()
        ):
            self.do_connect()
        else:
            self.show_login_widget()

    def get_creds_and_login(self):
        if self.OAuthSession.token and self.checkOAuthToken():
            if self.send_token(self.OAuthSession.token.get("access_token")):
                return
        QtWidgets.QMessageBox.warning(
            self, "Log In", "OAuth token verification failed, please relogin",
        )
        self.show_login_widget()

    def createOAuthSession(self):
        client_id = config.Settings.get("oauth/client_id")
        refresh_kwargs = dict(client_id=client_id)
        redirect_uri = config.Settings.get("oauth/redirect_uri")
        scope = config.Settings.get("oauth/scope")
        app_client = WebApplicationClient(client_id=client_id)
        OAuth = OAuth2Session(
            client=app_client,
            redirect_uri=redirect_uri,
            scope=scope,
            auto_refresh_kwargs=refresh_kwargs,
        )
        return OAuth

    def checkOAuthToken(self):
        if self.OAuthSession.token.get("expires_at", 0) < time.time() + 5:
            self.tokenTimer.stop()
            logger.info("Token expired, going to refresh")
            return self.refreshOAuthToken()
        return True

    def refreshOAuthToken(self):
        token_url = config.Settings.get('oauth/host') + OAUTH_TOKEN_PATH
        if not self.OAuthSession:
            self.OAuthSession = self.createOAuthSession()
        try:
            logger.debug("Refreshing OAuth token")
            token = self.OAuthSession.refresh_token(
                token_url,
                refresh_token=self.refresh_token,
                verify=False,
            )
            self.saveOAuthToken(token)
            return True
        except BaseException:
            logger.error("Error during refreshing token")
            return False

    def saveOAuthToken(self, token):
        config.Settings.set("oauth/token", token, persist=False)
        self.refresh_token = token.get("refresh_token")
        self.tokenTimer.start(1 * 1000)

    def show_login_widget(self):
        login_widget = LoginWidget(self.remember)
        login_widget.finished.connect(self.on_widget_login_data)
        login_widget.rejected.connect(self.on_widget_no_login)
        login_widget.request_quit.connect(
            self.on_login_widget_quit, QtCore.Qt.QueuedConnection,
        )
        login_widget.remember.connect(self.set_remember)
        login_widget.exec_()

    def on_widget_login_data(self, api_changed):
        self.lobby_connection.setHostFromConfig()
        self.lobby_connection.setPortFromConfig()
        self._chatMVC.connection.setHostFromConfig()
        self._chatMVC.connection.setPortFromConfig()
        if api_changed:
            self.ladder.refreshLeaderboards()
            self.map_downloader.update_url_prefix()
            self.news.updateNews()
            self.games.refreshMods()

        oauth_host = config.Settings.get("oauth/host")
        authorization_endpoint = oauth_host + OAUTH_AUTH_PATH
        self.OAuthSession = self.createOAuthSession()
        authorization_url, oauth_state = self.OAuthSession.authorization_url(
            authorization_endpoint,
        )
        oauth_widget = OAuthWidget(
            oauth_state=oauth_state,
            url=authorization_url,
        )
        oauth_widget.finished.connect(self.oauth_finished)
        oauth_widget.rejected.connect(self.on_widget_no_login)
        oauth_widget.exec_()

    def oauth_finished(self, state, code, error):
        token_url = config.Settings.get("oauth/host") + OAUTH_TOKEN_PATH
        if state:
            try:
                logger.debug("Fetching OAuth token")
                token = self.OAuthSession.fetch_token(
                    token_url,
                    code=code,
                    include_client_id=True,
                    verify=False,
                )
                self.saveOAuthToken(token)
                self.do_connect()
                return
            except BaseException:
                logger.error(
                    "Fetching token failed: ",
                    exc_info=sys.exc_info(),
                )
        elif error:
            logger.error("Error during logging in: {}".format(error))

        QtWidgets.QMessageBox.warning(
            self, "Log In", "Error occured, please retry",
        )
        self.on_widget_no_login()

    def on_widget_no_login(self):
        self.state = ClientState.DISCONNECTED

    def on_login_widget_quit(self):
        QtWidgets.QApplication.quit()

    def send_token(self, token):
        # Send data once we have the creds.
        self._autorelogin = False  # Fresh credentials
        self.unique_id = util.uniqueID(self.session)
        if not self.unique_id:
            QtWidgets.QMessageBox.critical(
                self,
                "Failed to calculate UID",
                "Failed to calculate your unique ID"
                " (a part of our smurf prevention system).\n"
                "It is very likely this happens due to your antivirus software"
                " deleting the faf-uid.exe file. If this has happened, please "
                "add an exception and restore the file. The file "
                "can also be restored by installing the client again.",
            )
            return False
        self.lobby_connection.send(
            dict(
                command="auth",
                token=token,
                unique_id=self.unique_id,
                session=self.session,
            ),
        )
        return True

    @QtCore.pyqtSlot()
    def started_fa(self):
        """
        Slot hooked up to fa.instance when the process has launched.
        It will notify other modules through the signal gameEnter().
        """
        logger.info("FA has launched in an attached process.")
        self.game_enter.emit()

    @QtCore.pyqtSlot(int)
    def finished_fa(self, exit_code):
        """
        Slot hooked up to fa.instance when the process has ended.
        It will notify other modules through the signal gameExit().
        """
        if not exit_code:
            logger.info("FA has finished with exit code: {}".format(exit_code))
        else:
            logger.warning(
                "FA has finished with exit code: {}".format(exit_code),
            )
        self.game_exit.emit()

    @QtCore.pyqtSlot(QtCore.QProcess.ProcessError)
    def error_fa(self, error_code):
        """
        Slot hooked up to fa.instance when the process has failed to start.
        """
        logger.error("FA has died with error: " + fa.instance.errorString())
        if error_code == 0:
            logger.error("FA has failed to start")
            QtWidgets.QMessageBox.critical(
                self, "Error from FA", "FA has failed to start.",
            )
        elif error_code == 1:
            logger.error("FA has crashed or killed after starting")
        else:
            text = (
                "FA has failed to start with error code: {}"
                .format(error_code)
            )
            logger.error(text)
            QtWidgets.QMessageBox.critical(self, "Error from FA", text)
        self.game_exit.emit()

    def tab_changed(self, tab, curr, prev):
        """
        The main visible tab (module) of the client's UI has changed.
        In this case, other modules may want to load some data or cease
        particularly CPU-intensive interactive functionality.
        """
        new_tab = tab.widget(curr)
        old_tab = tab.widget(prev)

        if old_tab is not None:
            tab = old_tab.layout().itemAt(0).widget()
            if isinstance(tab, BusyWidget):
                tab.busy_left()
        if new_tab is not None:
            tab = new_tab.layout().itemAt(0).widget()
            if isinstance(tab, BusyWidget):
                tab.busy_entered()
        # FIXME - special concession for chat tab. In the future we should
        # separate widgets from controlling classes, just like chat tab does -
        # then we'll refactor this part.
        if new_tab is self.chatTab:
            self._chatMVC.view.entered()

    @QtCore.pyqtSlot(int)
    def main_tab_changed(self, curr):
        self.tab_changed(self.mainTabs, curr, self._main_tab)
        self._main_tab = curr

    @QtCore.pyqtSlot(int)
    def vault_tab_changed(self, curr):
        self.tab_changed(self.topTabs, curr, self._vault_tab)
        self._vault_tab = curr

    def view_replays(self, name, leaderboardName=None):
        self.replays.set_player(name, leaderboardName)
        self.mainTabs.setCurrentIndex(self.mainTabs.indexOf(self.replaysTab))

    def view_in_leaderboards(self, user):
        self.ladder.setCurrentIndex(
            self.ladder.indexOf(self.ladder.leaderboardsTab),
        )
        self.ladder.leaderboards.widget(0).searchPlayerInLeaderboard(user)
        self.ladder.leaderboards.widget(1).searchPlayerInLeaderboard(user)
        self.ladder.leaderboards.widget(2).searchPlayerInLeaderboard(user)
        self.ladder.leaderboards.setCurrentIndex(1)
        self.mainTabs.setCurrentIndex(self.mainTabs.indexOf(self.ladderTab))

    def manage_power(self):
        """ update the interface accordingly to the power of the user """
        if self.power_tools.power >= 1:
            if self.mod_menu is None:
                self.mod_menu = self.menu.addMenu("Administration")

            action_lobby_kick = QtWidgets.QAction(
                "Close player's FAF Client...", self.mod_menu,
            )
            action_lobby_kick.triggered.connect(self._on_lobby_kick_triggered)
            self.mod_menu.addAction(action_lobby_kick)

            action_close_fa = QtWidgets.QAction(
                "Close Player's Game...",
                self.mod_menu,
            )
            action_close_fa.triggered.connect(self._close_game_dialog)
            self.mod_menu.addAction(action_close_fa)

    def _close_game_dialog(self):
        self.power_tools.view.close_game_dialog.show()

    # Needed so that we ignore the bool from the triggered() signal
    def _on_lobby_kick_triggered(self):
        self.power_tools.view.kick_dialog()

    def close_fa(self, username):
        self.power_tools.actions.close_fa(username)

    def handle_session(self, message):
        self.session = str(message['session'])
        self.get_creds_and_login()

    def handle_welcome(self, message):
        self.state = ClientState.LOGGED_IN
        self._auto_relogin = True
        self.id = message["me"]["id"]
        self.login = message["me"]["login"]

        self.me.onLogin(self.login, self.id)
        logger.info("Login success")

        util.crash.CRASH_REPORT_USER = self.login

        self.update_options()

        self.authorized.emit(self.me)

        if self.game_session is None or self.game_session.game_uid is None:
            self.game_session = GameSession(
                player_id=self.id,
                player_login=self.login,
            )
        elif self.game_session.game_uid is not None:
            self.lobby_connection.send({
                'command': 'restore_game_session',
                'game_id': self.game_session.game_uid,
            })

        self.game_session.gameFullSignal.connect(self.emit_game_full)

    def handle_irc_password(self, message):
        self.irc_password = message.get("password", "")

    def handle_registration_response(self, message):
        if message["result"] == "SUCCESS":
            return

        self.handle_notice({"style": "notice", "text": message["error"]})

    def ladderWarningClicked(self, faction=Factions.RANDOM):
        subFactions = [False] * 4
        if faction != Factions.RANDOM:
            subFactions[faction.value - 1] = True
        config.Settings.set(
            "play/{}Factions".format(MatchmakerQueueType.LADDER.value),
            subFactions,
        )
        try:
            self.games.matchmakerQueues.widget(0).subFactions = subFactions
            self.games.matchmakerQueues.widget(0).setFactionIcons(subFactions)
            self.games.matchmakerQueues.widget(0).startSearchRanked()
        except BaseException:
            QtWidgets.QMessageBox.information(
                self, "Starting search failed",
                "Something went wrong, please retry",
            )

    def search_ranked(self, queue_name):
        msg = {
            'command': 'game_matchmaking',
            'queue_name': queue_name,
            'state': 'start',
        }
        self.lobby_connection.send(msg)

    def handle_match_found_message(self, message):
        logger.info("Handling match_found via JSON {}".format(message))
        self.warningHide()
        self.labelAutomatchInfo.setText("Match found! Pending game launch...")
        self.labelAutomatchInfo.show()
        self.games.handleMatchFound(message)
        self.lobby_connection.send(dict(command="match_ready"))

    def handle_match_cancelled(self, message):
        logger.info("Received match_cancelled via JSON {}".format(message))
        self.labelAutomatchInfo.setText("")
        self.labelAutomatchInfo.hide()
        self.games.handleMatchCancelled(message)

    def host_game(
        self,
        title,
        mod,
        visibility,
        mapname,
        password,
        is_rehost=False,
    ):
        msg = {
            'command': 'game_host',
            'title': title,
            'mod': mod,
            'visibility': visibility,
            'mapname': mapname,
            'password': password,
            'is_rehost': is_rehost,
        }
        self.lobby_connection.send(msg)

    def join_game(self, uid, password=None):
        msg = {
            'command': 'game_join',
            'uid': uid,
            'gameport': 0,
        }
        if password:
            msg['password'] = password
        self.lobby_connection.send(msg)

    def handle_game_launch(self, message):

        self.game_session.startIceAdapter()

        logger.info("Handling game_launch via JSON {}".format(message))

        silent = False
        # Do some special things depending of the reason of the game launch.

        # HACK: Ideally, this comes from the server, too.
        # LATER: search_ranked message
        arguments = []
        if self.games.matchFoundQueueName:
            self.labelAutomatchInfo.setText("Launching the game...")
            ratingType = message.get("rating_type", RatingType.GLOBAL.value)
            factionSubset = config.Settings.get(
                "play/{}Factions".format(self.games.matchFoundQueueName),
                default=[False] * 4,
                type=bool,
            )
            faction = Factions.set_faction(factionSubset)
            arguments.append('/' + Factions.to_name(faction))
            # Player rating
            arguments.append('/mean')
            arguments.append(
                str(self.me.player.rating_mean(ratingType)),
            )
            arguments.append('/deviation')
            arguments.append(
                str(self.me.player.rating_deviation(ratingType)),
            )

            arguments.append('/players')
            arguments.append(str(message["expected_players"]))
            arguments.append('/team')
            arguments.append(str(message["team"]))
            arguments.append('/startspot')
            arguments.append(str(message["map_position"]))
            if message.get("game_options"):
                arguments.append('/gameoptions')
                for key, value in message["game_options"].items():
                    arguments.append('{}:{}'.format(key, value))

            # Launch the auto lobby
            self.game_session.setLobbyInitMode("auto")
        else:
            # Player global rating
            arguments.append('/mean')
            arguments.append(str(self.me.player.global_rating_mean))
            arguments.append('/deviation')
            arguments.append(str(self.me.player.global_rating_deviation))
            if self.me.player.country is not None:
                arguments.append('/country ')
                arguments.append(self.me.player.country)

            # Launch the normal lobby
            self.game_session.setLobbyInitMode("normal")

        arguments.append('/numgames')
        arguments.append(str(message["args"][1]))

        if self.me.player.clan is not None:
            arguments.append('/clan')
            arguments.append(self.me.player.clan)

        # Ensure we have the map
        if "mapname" in message:
            fa.check.map_(message['mapname'], force=True, silent=silent)

        if "sim_mods" in message:
            fa.mods.checkMods(message['sim_mods'])

        info = dict(
            uid=message['uid'],
            recorder=self.login,
            featured_mod=message['mod'],
            launched_at=time.time(),
        )

        self.game_session.game_uid = message['uid']

        fa.run(
            info, self.game_session.relay_port, self.replayServer.serverPort(),
            arguments, self.game_session.game_uid,
        )

    def fill_in_session_info(self, game):
        # sometimes we get the game_info message before a game session was
        # created
        if self.game_session and game.uid == self.game_session.game_uid:
            self.game_session.game_map = game.mapname
            self.game_session.game_mod = game.featured_mod
            self.game_session.game_name = game.title
            self.game_session.game_visibility = game.visibility.value

    def handle_matchmaker_info(self, message):
        logger.debug(
            "Handling matchmaker info with message {}".format(message),
        )
        if not self.me.player:
            return
        self.matchmaker_info.emit(message)
        if "queues" in message:
            show = None
            for q in message['queues']:
                if q['queue_name'] == 'ladder1v1':
                    show = False
                    mu = self.me.player.ladder_rating_mean
                    if self.me.player.ladder_rating_deviation < 100:
                        key = 'boundary_80s'
                    else:
                        key = 'boundary_75s'
                    for min, max in q[key]:
                        if min < mu < max:
                            show = True
            if (
                self.me.player.ladder_rating_deviation > 200
                or self.games.searching.get("ladder1v1", False)
            ):
                return
            if show is not None:
                if show and not self.games.matchFoundQueueName:
                    self.warningShow()
                else:
                    self.warningHide()

    def handle_social(self, message):
        if "channels" in message:
            # Add a delay to the notification system (insane cargo cult)
            self.notificationSystem.disabledStartup = False
            self.channels_updated.emit(message["channels"])

        if "power" in message:
            self.power_tools.power = message["power"]
            self.manage_power()

    def handle_player_info(self, message):
        players = message["players"]

        # Fix id being a Python keyword
        for player in players:
            player["id_"] = player["id"]
            del player["id"]

        for player in players:
            id_ = int(player["id_"])
            logger.debug('Received update about player {}'.format(id_))
            if id_ in self.players:
                self.players[id_].update(**player)
            else:
                self.players[id_] = Player(**player)

    def handle_authentication_failed(self, message):
        QtWidgets.QMessageBox.warning(
            self, "Authentication failed", message["text"],
        )
        self._auto_relogin = False
        self.disconnect_()
        self.show_login_widget()

    def handle_notice(self, message):
        if "text" in message:
            style = message.get('style', None)
            if style == "error":
                logger.error(
                    "Received an error message from server: {}"
                    .format(message),
                )
                QtWidgets.QMessageBox.critical(
                    self, "Error from Server", message["text"],
                )
            elif style == "warning":
                logger.warning(
                    "Received warning message from server: {}".format(message),
                )
                QtWidgets.QMessageBox.warning(
                    self, "Warning from Server", message["text"],
                )
            elif style == "scores":
                self.tray.showMessage(
                    "Scores", message["text"],
                    QtWidgets.QSystemTrayIcon.Information, 3500,
                )
                self.local_broadcast.emit("Scores", message["text"])
            elif "You are using an unofficial client" in message["text"]:
                # self.unofficial_client.emit(message["text"])
                {}
            else:
                QtWidgets.QMessageBox.information(
                    self, "Notice from Server", message["text"],
                )

        if message["style"] == "kill":
            logger.info("Server has killed your Forged Alliance Process.")
            fa.instance.kill()

        if message["style"] == "kick":
            logger.info("Server has kicked you from the Lobby.")

        # This is part of the protocol - in this case we should not relogin
        # automatically.
        if message["style"] in ["error", "kick"]:
            self._auto_relogin = False

    def handle_invalid(self, message):
        # We did something wrong and the server will disconnect, let's not
        # reconnect and potentially cause the same error again and again
        self.lobby_reconnector.enabled = False
        raise Exception(message)

    def emit_game_full(self):
        self.game_full.emit()

    def invite_to_party(self, recipient_id):
        self.games.stopSearch()
        msg = {
            'command': 'invite_to_party',
            'recipient_id': recipient_id,
        }
        self.lobby_connection.send(msg)

    def handle_party_invite(self, message):
        logger.info("Handling party_invite via JSON {}".format(message))
        self.party_invite.emit(message)

    def handle_update_party(self, message):
        logger.info("Handling update_party via JSON {}".format(message))
        self.games.updateParty(message)

    def handle_kicked_from_party(self, message):
        if self.me.player and self.me.player.currentGame is None:
            QtWidgets.QMessageBox.information(
                self, "Kicked", "You were kicked from party",
            )
        msg = {
            "owner": self.me.id,
            "members": [
                {
                    "player": self.me.id,
                    "factions": ["uef", "cybran", "aeon", "seraphim"],
                },
            ],
        }
        self.games.updateParty(msg)

    def set_faction(self, faction):
        logger.info("Setting party factions to {}".format(faction))
        msg = {
            'command': 'set_party_factions',
            'factions': faction,
        }
        self.lobby_connection.send(msg)

    def handle_search_info(self, message):
        logger.info("Handling search_info via JSON: {}".format(message))
        self.games.handleMatchmakerSearchInfo(message)
