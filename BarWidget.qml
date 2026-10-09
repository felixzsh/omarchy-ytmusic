import QtQuick
import QtQuick.Effects
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

import "Api.js" as Api

BarWidget {
  id: root

  moduleName: "felixzsh.ytmusic"

  readonly property var ytmusic: bar && bar.shell
    ? bar.shell.serviceFor("felixzsh.ytmusic") : null
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property string surfaceKey: "ytmusic-popup-" + String(root)
  readonly property string lyricsRequestKey: surfaceKey + "-lyrics"
  readonly property bool miniPlayerEnabled:
    String(root.setting("showMiniPlayer", "On")) !== "Off"
  property bool popupOpen: false
  property bool lyricsInstallPromptVisible: false
  property bool miniShortcutHelpVisible: false
  property bool popoutSwitchClosing: false
  property bool miniCursorActive: false
  property string miniCursor: "play"
  property real volumeBeforeMute: 0.5
  readonly property bool opened: popupOpen
  readonly property url iconSource: Qt.resolvedUrl("assets/ytmusic.svg")
  readonly property var miniShortcutRows: [
    { keys: "Tab / arrows / HJKL", action: "Select a control" },
    { keys: "Enter", action: "Activate selected button" },
    { keys: "Left / Right", action: "Adjust selected slider" },
    { keys: "Space", action: "Play or pause" },
    { keys: "Ctrl+Left / Right", action: "Previous or next track" },
    { keys: "Shift+Left / Right", action: "Seek 10 seconds" },
    { keys: "Ctrl+Up / Down", action: "Change volume" },
    { keys: "M", action: "Mute or restore volume" },
    { keys: "Ctrl+S / Ctrl+R", action: "Shuffle / repeat" },
    { keys: "Ctrl+Shift+L", action: "Open lyrics" },
    { keys: "O", action: "Open full player" },
    { keys: "Ctrl+/", action: "Toggle this reference" },
    { keys: "Scroll the bar icon", action: "Previous or next track" },
    { keys: "Middle-click the bar icon", action: "Play or pause" },
    { keys: "Esc", action: "Close" }
  ]
  readonly property var miniKeyboardActions: {
    if (lyricsInstallPromptVisible) return ["prompt-cancel", "prompt-confirm"]
    if (miniShortcutHelpVisible) return ["help-close"]
    if (ytmusic && !ytmusic.accountConnected) {
      var setupActions = ["setup"]
      setupActions.push("open")
      return setupActions
    }
    var actions = []
    if (ytmusic && ytmusic.currentArtistContextAvailable) actions.push("artist")
    if (ytmusic && ytmusic.currentTrackSaveAvailable) actions.push("like")
    if (ytmusic && ytmusic.lengthSeconds > 0
        && ytmusic.playbackControllable) actions.push("seek")
    if (ytmusic && ytmusic.playbackControllable) {
      actions.push("shuffle", "previous", "play", "next", "repeat")
    }
    if (ytmusic && ytmusic.lyricsAvailable) actions.push("lyrics")
    if (ytmusic && ytmusic.hasPlayer && ytmusic.volumeSupported)
      actions.push("volume")
    actions.push("open")
    return actions
  }

  function open() { popupOpen = true }
  function close() {
    miniShortcutHelpVisible = false
    popupOpen = false
  }
  function closeForPopoutSwitch() {
    popoutSwitchClosing = true
    close()
    Qt.callLater(function() { root.popoutSwitchClosing = false })
  }
  function toggle() {
    if (miniPlayerEnabled) popupOpen ? close() : open()
    else openFullPanel()
  }

  function shortcutPlayer() {
    return Api.normalizedShortcutPlayer(root.setting("shortcutPlayer",
      "Omarchy Music app"))
  }

  function toggleMiniPlayerShortcut() {
    if (!bar || typeof bar.isBarWidgetOpen !== "function"
        || typeof bar.hideBarWidget !== "function"
        || typeof bar.summonBarWidget !== "function") return "unavailable"
    if (bar.isBarWidgetOpen(moduleName))
      return bar.hideBarWidget(moduleName) ? "closed" : "unavailable"
    var host = bar.shell
    if (host && typeof host.isPluginOpen === "function"
        && host.isPluginOpen(moduleName) && typeof host.hide === "function") {
      host.hide(moduleName)
      Qt.callLater(function() {
        if (root.bar) root.bar.summonBarWidget(root.moduleName)
      })
      return "opened"
    }
    return bar.summonBarWidget(moduleName) ? "opened" : "unavailable"
  }

  function toggleFullPlayerShortcut() {
    var host = bar ? bar.shell : null
    if (!host || typeof host.isPluginOpen !== "function"
        || typeof host.hide !== "function"
        || typeof host.summon !== "function") return "unavailable"
    if (host.isPluginOpen(moduleName)) {
      host.hide(moduleName)
      return "closed"
    }
    if (bar && typeof bar.isBarWidgetOpen === "function"
        && bar.isBarWidgetOpen(moduleName)
        && typeof bar.hideBarWidget === "function") {
      bar.hideBarWidget(moduleName)
      Qt.callLater(function() {
        if (root.bar && root.bar.shell)
          root.bar.shell.summon(root.moduleName, "{}")
      })
      return "opened"
    }
    return host.summon(moduleName, "{}") ? "opened" : "unavailable"
  }

  function toggleConfiguredPlayerShortcut() {
    var target = shortcutPlayer()
    if (target === "Full player") return toggleFullPlayerShortcut()
    if (target === "Mini player") return toggleMiniPlayerShortcut()
    if (!bar || typeof bar.run !== "function") return "unavailable"
    bar.run("omarchy launch youtube-music || xdg-open https://music.youtube.com")
    return "launched"
  }

  function openFullPanel(payload) {
    close()
    if (!bar || !bar.shell) return
    var encoded = JSON.stringify(payload || ({}))
    if (typeof bar.shell.hide === "function"
        && typeof bar.shell.summon === "function") {
      bar.shell.hide("felixzsh.ytmusic")
      Qt.callLater(function() {
        if (root.bar && root.bar.shell)
          root.bar.shell.summon("felixzsh.ytmusic", encoded)
      })
    } else if (payload && typeof bar.shell.summon === "function")
      bar.shell.summon("felixzsh.ytmusic", encoded)
    else bar.shell.toggle("felixzsh.ytmusic", encoded)
  }

  IpcHandler {
    target: root.moduleName + ".player"

    function configuredPlayer(): string {
      return root.shortcutPlayer()
    }
    function togglePlayer(): string {
      return root.toggleConfiguredPlayerShortcut()
    }
    function toggleMiniPlayer(): string {
      return root.toggleMiniPlayerShortcut()
    }
    function toggleFullPlayer(): string {
      return root.toggleFullPlayerShortcut()
    }
    function debugLookup(): string {
      var shell = root.bar ? root.bar.shell : null
      var lookup = shell ? shell._serviceLookup : null
      return JSON.stringify({
        hasBar: !!root.bar,
        moduleName: root.moduleName,
        barModuleName: root.bar ? root.bar.moduleName : "",
        barPluginId: root.bar ? root.bar.pluginId : "",
        hasShell: !!shell,
        shellPluginId: shell ? String(shell.pluginId) : "",
        hasServiceFor: !!(shell && typeof shell.serviceFor === "function"),
        hasLookup: typeof lookup === "function",
        hasSummon: !!(shell && typeof shell.summon === "function"),
        hasModuleWidgets: !!(root.bar && typeof root.bar.moduleWidgets === "function"),
        hasFirstPartyServiceFor: !!(shell && typeof shell.firstPartyServiceFor === "function"),
        serviceValue: shell && typeof shell.serviceFor === "function"
          ? String(shell.serviceFor("felixzsh.ytmusic")) : "n/a",
        ytmusicNull: root.ytmusic === null
      })
    }
    function debugOpenMini(): string {
      root.open()
      return root.popupOpen ? "open" : "closed"
    }
    function debugCloseMini(): string {
      root.close()
      return "closed"
    }
  }

  function openCurrentArtist() {
    if (!ytmusic || !ytmusic.currentArtistContextAvailable) return
    ytmusic.currentContext("artist", function(item) {
      if (item) root.openFullPanel({ tab: "detail", detailItem: item })
    })
  }

  function openLyrics() {
    if (!ytmusic || !ytmusic.currentLyricsSong) return
    var result = ytmusic.requestLyrics(lyricsRequestKey)
    if (result !== "opening") {
      lyricsInstallPromptVisible = true
      popupOpen = true
    }
  }

  function dismissLyricsInstallPrompt() {
    if (ytmusic) ytmusic.cancelLyricsPlugin(lyricsRequestKey)
    lyricsInstallPromptVisible = false
  }

  function toggleMiniShortcutHelp() {
    if (lyricsInstallPromptVisible) return
    miniShortcutHelpVisible = !miniShortcutHelpVisible
    if (miniShortcutHelpVisible) setMiniCursor("help-close")
    else ensureMiniCursor()
  }

  function ensureMiniCursor() {
    var actions = miniKeyboardActions
    if (!actions.length) {
      miniCursorActive = false
      return
    }
    if (actions.indexOf(miniCursor) >= 0) return
    miniCursor = actions.indexOf("play") >= 0 ? "play" : actions[0]
  }

  function setMiniCursor(action) {
    if (miniKeyboardActions.indexOf(action) < 0) return
    miniCursor = action
    miniCursorActive = true
  }

  function moveMiniCursor(delta) {
    var actions = miniKeyboardActions
    if (!actions.length) return
    var index = actions.indexOf(miniCursor)
    if (index < 0) index = actions.indexOf("play")
    if (index < 0) index = 0
    index = (index + (delta < 0 ? -1 : 1) + actions.length) % actions.length
    miniCursor = actions[index]
    miniCursorActive = true
  }

  function seekBy(seconds) {
    if (!ytmusic || !ytmusic.playbackControllable) return
    ytmusic.seekSeconds(Api.seekPosition(ytmusic.positionSeconds, seconds,
      ytmusic.lengthSeconds))
  }

  function adjustVolume(delta) {
    if (!ytmusic || !ytmusic.volumeSupported) return
    var next = Api.nextVolume(ytmusic.volume, delta)
    if (Api.shouldRememberVolume(next)) volumeBeforeMute = next
    ytmusic.setVolume(next)
  }

  function toggleMute() {
    if (!ytmusic || !ytmusic.volumeSupported) return
    var current = Api.nextVolume(ytmusic.volume, 0)
    if (Api.shouldRememberVolume(current)) {
      volumeBeforeMute = current
      ytmusic.setVolume(0)
    } else ytmusic.setVolume(Api.unmuteVolume(volumeBeforeMute))
  }

  function activateMiniAction(action) {
    if (action === "help-close") toggleMiniShortcutHelp()
    else if (action === "prompt-cancel") dismissLyricsInstallPrompt()
    else if (action === "prompt-confirm") {
      if (ytmusic && !ytmusic.lyricsPluginBusy)
        ytmusic.confirmLyricsPlugin(lyricsRequestKey)
    } else if (action === "artist") openCurrentArtist()
    else if (action === "like") {
      if (ytmusic) ytmusic.toggleCurrentTrackSaved()
    } else if (action === "shuffle") {
      if (ytmusic) ytmusic.setShuffle(!ytmusic.shuffle)
    } else if (action === "previous") {
      if (ytmusic) ytmusic.previous()
    } else if (action === "play") {
      if (ytmusic) ytmusic.togglePlayback()
    } else if (action === "next") {
      if (ytmusic) ytmusic.next()
    } else if (action === "repeat") {
      if (ytmusic) ytmusic.cycleRepeat()
    } else if (action === "lyrics") openLyrics()
    else if (action === "volume") toggleMute()
    else if (action === "setup") {
      if (ytmusic) ytmusic.login()
      openFullPanel({ tab: "login" })
    } else if (action === "open") openFullPanel()
  }

  function handleMiniKey(event) {
    var ctrl = (event.modifiers & Qt.ControlModifier) !== 0
    var shift = (event.modifiers & Qt.ShiftModifier) !== 0
    var alt = (event.modifiers & Qt.AltModifier) !== 0
    var plain = !ctrl && !shift && !alt
    var text = String(event.text || "").toLowerCase()

    if (lyricsInstallPromptVisible) {
      if (event.key === Qt.Key_Escape) dismissLyricsInstallPrompt()
      else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab)
        moveMiniCursor(shift || event.key === Qt.Key_Backtab ? -1 : 1)
      else if (event.key === Qt.Key_Left || event.key === Qt.Key_Up
          || text === "h" || text === "k") moveMiniCursor(-1)
      else if (event.key === Qt.Key_Right || event.key === Qt.Key_Down
          || text === "l" || text === "j") moveMiniCursor(1)
      else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
          || event.key === Qt.Key_Space) {
        if (!event.isAutoRepeat) activateMiniAction(miniCursor)
      } else return
      event.accepted = true
      return
    }

    if (miniShortcutHelpVisible) {
      if (event.key === Qt.Key_Escape
          || event.key === Qt.Key_Return || event.key === Qt.Key_Enter
          || event.key === Qt.Key_Space) {
        if (!event.isAutoRepeat) toggleMiniShortcutHelp()
        event.accepted = true
      }
      return
    }

    if (event.key === Qt.Key_Escape) close()
    else if (ctrl && event.key === Qt.Key_Left) { if (ytmusic) ytmusic.previous() }
    else if (ctrl && event.key === Qt.Key_Right) { if (ytmusic) ytmusic.next() }
    else if (shift && !ctrl && event.key === Qt.Key_Left) seekBy(-10)
    else if (shift && !ctrl && event.key === Qt.Key_Right) seekBy(10)
    else if (ctrl && event.key === Qt.Key_Up) adjustVolume(0.05)
    else if (ctrl && event.key === Qt.Key_Down) adjustVolume(-0.05)
    else if (ctrl && !shift && event.key === Qt.Key_S) {
      if (ytmusic && !event.isAutoRepeat) ytmusic.setShuffle(!ytmusic.shuffle)
    } else if (ctrl && !shift && event.key === Qt.Key_R) {
      if (ytmusic && !event.isAutoRepeat) ytmusic.cycleRepeat()
    } else if (ctrl && shift && event.key === Qt.Key_L) {
      if (!event.isAutoRepeat) openLyrics()
    } else if (plain && event.key === Qt.Key_Space) {
      if (ytmusic && !event.isAutoRepeat) ytmusic.togglePlayback()
    } else if (plain && event.key === Qt.Key_M) {
      if (!event.isAutoRepeat) toggleMute()
    } else if (plain && event.key === Qt.Key_O) {
      if (!event.isAutoRepeat) openFullPanel()
    } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
      moveMiniCursor(shift || event.key === Qt.Key_Backtab ? -1 : 1)
    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      if (!event.isAutoRepeat) activateMiniAction(miniCursor)
    } else if (plain && (event.key === Qt.Key_Left || text === "h")) {
      if (miniCursor === "seek") seekBy(-5)
      else if (miniCursor === "volume") adjustVolume(-0.05)
      else moveMiniCursor(-1)
    } else if (plain && (event.key === Qt.Key_Right || text === "l")) {
      if (miniCursor === "seek") seekBy(5)
      else if (miniCursor === "volume") adjustVolume(0.05)
      else moveMiniCursor(1)
    } else if (plain && (event.key === Qt.Key_Up || text === "k")) moveMiniCursor(-1)
    else if (plain && (event.key === Qt.Key_Down || text === "j")) moveMiniCursor(1)
    else if (plain && event.key === Qt.Key_Home) setMiniCursor(miniKeyboardActions[0])
    else if (plain && event.key === Qt.Key_End)
      setMiniCursor(miniKeyboardActions[miniKeyboardActions.length - 1])
    else return
    event.accepted = true
  }

  function syncSettings() {
    if (ytmusic) ytmusic.applySettings(settings)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onSettingsChanged: syncSettings()
  onYtmusicChanged: syncSettings()
  onMiniPlayerEnabledChanged: if (!miniPlayerEnabled) close()
  onMiniKeyboardActionsChanged: ensureMiniCursor()
  onLyricsInstallPromptVisibleChanged: {
    if (lyricsInstallPromptVisible) {
      miniCursor = "prompt-cancel"
      miniCursorActive = true
    } else ensureMiniCursor()
  }
  onPopupOpenChanged: {
    if (popupOpen) {
      miniCursor = miniKeyboardActions.indexOf("play") >= 0
        ? "play" : miniKeyboardActions[0]
      miniCursorActive = miniKeyboardActions.length > 0
    } else miniCursorActive = false
    if (ytmusic) ytmusic.setUiVisible(surfaceKey, popupOpen)
    if (!popupOpen && lyricsInstallPromptVisible
        && (!ytmusic || !ytmusic.lyricsPluginBusy)) {
      if (ytmusic) ytmusic.cancelLyricsPlugin(lyricsRequestKey)
      lyricsInstallPromptVisible = false
    }
  }
  Component.onCompleted: syncSettings()
  Component.onDestruction: if (ytmusic) ytmusic.setUiVisible(surfaceKey, false)

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    fontSize: Style.font.body
    active: root.ytmusic && root.ytmusic.playing
    activeColor: button.foreground
    tooltipText: root.ytmusic && root.ytmusic.hasMedia
      ? root.ytmusic.title + (root.ytmusic.artist ? " — " + root.ytmusic.artist : "")
      : (root.ytmusic && !root.ytmusic.accountConnected
        ? "Set up Omarchy YouTube Music" : "Omarchy YouTube Music")
    fixedWidth: root.vertical ? root.barSize : Style.bar.statusSlot
    fixedHeight: root.vertical ? Style.bar.statusSlot : -1
    clip: true

    Row {
      id: barContent
      anchors.centerIn: parent
      spacing: Style.space(6)
      visible: !root.vertical
      enabled: false

      Item {
        id: barGlyph
        width: Style.bar.iconCanvas
        height: Style.bar.iconCanvas
        anchors.verticalCenter: parent.verticalCenter

        Image {
          id: barIcon
          anchors.centerIn: parent
          width: Style.bar.iconFont
          height: Style.bar.iconFont
          source: root.iconSource
          sourceSize.width: Style.bar.iconFont * 2
          sourceSize.height: Style.bar.iconFont * 2
          fillMode: Image.PreserveAspectFit
          visible: false
          layer.enabled: true
        }

        MultiEffect {
          anchors.fill: barIcon
          source: barIcon
          colorization: 1.0
          colorizationColor: button.foreground
        }
      }
    }

    Item {
      visible: button.vertical
      anchors.centerIn: parent
      width: Style.bar.iconCanvas
      height: Style.bar.iconCanvas

      Image {
        id: verticalIcon
        anchors.centerIn: parent
        width: Style.bar.iconFont
        height: Style.bar.iconFont
        source: root.iconSource
        visible: false
        layer.enabled: true
      }
      MultiEffect {
        anchors.fill: verticalIcon
        source: verticalIcon
        colorization: 1.0
        colorizationColor: button.foreground
      }
    }

    onPressed: function(mouseButton) {
      if (mouseButton === Qt.MiddleButton) {
        if (root.ytmusic) root.ytmusic.togglePlayback()
      } else {
        root.toggle()
      }
    }
    onWheelMoved: function(delta) {
      if (!root.ytmusic) return
      if (delta > 0) root.ytmusic.previous()
      else if (delta < 0) root.ytmusic.next()
    }
  }

  KeyboardPanel {
    id: popup
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.popupOpen
    focusTarget: miniKeyCatcher
    contentWidth: fittedContentWidth(Style.space(340))
    contentHeight: fittedContentHeight(root.miniShortcutHelpVisible
      ? miniShortcutHelp.implicitHeight : contentColumn.implicitHeight)

    Item {
      id: miniKeyCatcher
      anchors.fill: parent
      focus: true
      Keys.priority: Keys.BeforeItem
      Keys.onPressed: function(event) { root.handleMiniKey(event) }

      Shortcut {
        sequence: "Ctrl+/"
        enabled: root.popupOpen && !root.lyricsInstallPromptVisible
        onActivated: root.toggleMiniShortcutHelp()
      }

      Column {
        id: contentColumn
        anchors.fill: parent
        visible: !root.miniShortcutHelpVisible
        spacing: Style.space(10)

        Column {
          width: parent.width
          spacing: Style.space(8)
          visible: !root.lyricsInstallPromptVisible
            && root.ytmusic && !root.ytmusic.accountConnected

          Text {
            width: parent.width
            text: root.ytmusic ? root.ytmusic.loginProgress : "Starting YouTube Music…"
            color: root.foreground
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.body
            font.bold: true
            wrapMode: Text.WordWrap
          }

          Text {
            width: parent.width
            text: "Connect your YouTube Music account from the full player. Public home shelves work without signing in."
            color: Qt.darker(root.foreground, 1.4)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Row {
            width: parent.width
            spacing: Style.space(6)

            Button {
              text: "Set up and continue"
              iconText: "󰍂"
              foreground: root.foreground
              hasCursor: root.miniCursorActive && root.miniCursor === "setup"
              onClicked: root.activateMiniAction("setup")
              onHovered: function(on) { if (on) root.setMiniCursor("setup") }
            }
          }
        }

        Row {
          width: parent.width
          spacing: Style.space(12)
          visible: !root.lyricsInstallPromptVisible
            && (!root.ytmusic || root.ytmusic.accountConnected || root.ytmusic.hasMedia)

          BorderSurface {
            width: Style.space(78)
            height: width
            radius: Style.cornerRadius
            color: Style.normalFillFor(root.foreground, Commons.Color.accent)
            borderSpec: Border.controlSpec("normal", root.foreground, Commons.Color.accent)

            Image {
              id: popupArtwork
              anchors.fill: parent
              anchors.margins: Style.space(3)
              source: root.popupOpen && root.ytmusic ? root.ytmusic.artUrl : ""
              sourceSize.width: 156
              sourceSize.height: 156
              fillMode: Image.PreserveAspectFit
              asynchronous: true
              cache: true
              visible: status === Image.Ready
            }

            Text {
              anchors.centerIn: parent
              visible: popupArtwork.status !== Image.Ready
              text: "󰝚"
              color: root.foreground
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.displayLarge
            }
          }

          Column {
            width: parent.width - Style.space(90)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(4)

            Row {
              width: parent.width
              spacing: Style.space(3)

              Text {
                width: Math.max(20, parent.width
                  - (barCurrentTrackLikeButton.visible
                    ? barCurrentTrackLikeButton.width + parent.spacing : 0))
                anchors.verticalCenter: parent.verticalCenter
                text: root.ytmusic && root.ytmusic.title
                  ? root.ytmusic.title : "Nothing playing"
                color: root.foreground
                font.family: root.bar ? root.bar.fontFamily : Style.font.family
                font.pixelSize: Style.font.subtitle
                font.bold: true
                elide: Text.ElideRight
              }

              Button {
                id: barCurrentTrackLikeButton
                visible: root.ytmusic && !!root.ytmusic.currentTrackItem
                anchors.verticalCenter: parent.verticalCenter
                iconText: root.ytmusic && root.ytmusic.currentTrackSaved
                  ? "󰋑" : "󰋕"
                iconSize: Style.font.body
                foreground: root.foreground
                selected: root.ytmusic && root.ytmusic.currentTrackSaved
                hasCursor: root.miniCursorActive && root.miniCursor === "like"
                enabled: root.ytmusic && root.ytmusic.currentTrackSaveAvailable
                horizontalPadding: Style.space(4)
                verticalPadding: Style.space(2)
                tooltipText: root.ytmusic && root.ytmusic.currentTrackSaved
                  ? "Remove like" : "Like this song"
                onClicked: if (root.ytmusic) root.ytmusic.toggleCurrentTrackSaved()
                onHovered: function(on) { if (on) root.setMiniCursor("like") }
              }
            }

            Text {
              width: parent.width
              text: root.ytmusic ? root.ytmusic.artist : ""
              color: Qt.darker(root.foreground, 1.35)
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
              visible: text !== ""
            }

            Text {
              width: parent.width
              text: root.ytmusic ? root.ytmusic.album : ""
              color: Qt.darker(root.foreground, 1.55)
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
              visible: text !== ""
            }
          }
        }

        Column {
          width: parent.width
          spacing: Style.space(3)
          visible: !root.lyricsInstallPromptVisible
            && root.ytmusic && root.ytmusic.lengthSeconds > 0

          PlaybackSlider {
            id: miniSeekSlider
            width: parent.width
            bar: root.bar
            minimum: 0
            maximum: Math.max(1, root.ytmusic ? root.ytmusic.lengthSeconds : 1)
            sourceValue: root.ytmusic ? root.ytmusic.positionSeconds : 0
            sourcePending: root.ytmusic && root.ytmusic.pendingSeek !== null
            acknowledgeTolerance: 2
            contextKey: root.ytmusic ? root.ytmusic.currentUri : ""
            step: 5
            onCommitted: function(value) {
              if (root.ytmusic) root.ytmusic.seekSeconds(value)
            }
          }

          Row {
            width: parent.width
            Text {
              id: positionTime
              text: Api.millisecondsToClock((root.ytmusic ? root.ytmusic.positionSeconds : 0) * 1000)
              color: Qt.darker(root.foreground, 1.45)
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.caption
            }
            Item { width: Math.max(0, parent.width - positionTime.implicitWidth - endTime.implicitWidth); height: 1 }
            Text {
              id: endTime
              text: Api.millisecondsToClock((root.ytmusic ? root.ytmusic.lengthSeconds : 0) * 1000)
              color: Qt.darker(root.foreground, 1.45)
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
        }

        Row {
          anchors.horizontalCenter: parent.horizontalCenter
          spacing: Style.space(5)
          visible: !root.lyricsInstallPromptVisible

          Button {
            iconText: "󰒟"
            foreground: root.foreground
            selected: root.ytmusic && root.ytmusic.shuffle
            hasCursor: root.miniCursorActive && root.miniCursor === "shuffle"
            tooltipText: "Shuffle · Ctrl+S"
            enabled: root.ytmusic && root.ytmusic.playbackControllable
            onClicked: if (root.ytmusic) root.ytmusic.setShuffle(!root.ytmusic.shuffle)
            onHovered: function(on) { if (on) root.setMiniCursor("shuffle") }
          }
          Button {
            iconText: "󰒮"
            foreground: root.foreground
            hasCursor: root.miniCursorActive && root.miniCursor === "previous"
            tooltipText: "Previous · Ctrl+Left"
            enabled: root.ytmusic && root.ytmusic.playbackControllable
            onClicked: if (root.ytmusic) root.ytmusic.previous()
            onHovered: function(on) { if (on) root.setMiniCursor("previous") }
          }
          Button {
            iconText: root.ytmusic && root.ytmusic.playing ? "󰏤" : "󰐊"
            iconSize: Style.font.iconLarge
            foreground: root.foreground
            hasCursor: root.miniCursorActive && root.miniCursor === "play"
            tooltipText: (root.ytmusic && root.ytmusic.playing ? "Pause" : "Play") + " · Space"
            enabled: root.ytmusic && (root.ytmusic.playbackControllable || root.ytmusic.hasMedia)
            onClicked: if (root.ytmusic) root.ytmusic.togglePlayback()
            onHovered: function(on) { if (on) root.setMiniCursor("play") }
          }
          Button {
            iconText: "󰒭"
            foreground: root.foreground
            hasCursor: root.miniCursorActive && root.miniCursor === "next"
            tooltipText: "Next · Ctrl+Right"
            enabled: root.ytmusic && root.ytmusic.playbackControllable
            onClicked: if (root.ytmusic) root.ytmusic.next()
            onHovered: function(on) { if (on) root.setMiniCursor("next") }
          }
          Button {
            iconText: root.ytmusic && root.ytmusic.repeatMode === "track" ? "󰑘" : "󰑖"
            foreground: root.foreground
            selected: root.ytmusic && root.ytmusic.repeatMode !== "off"
            hasCursor: root.miniCursorActive && root.miniCursor === "repeat"
            tooltipText: "Repeat: " + Api.repeatModeLabel(root.ytmusic
              ? root.ytmusic.repeatMode : "off") + " · Ctrl+R"
            enabled: root.ytmusic && root.ytmusic.playbackControllable
            onClicked: if (root.ytmusic) root.ytmusic.cycleRepeat()
            onHovered: function(on) { if (on) root.setMiniCursor("repeat") }
          }
          Button {
            iconText: "󰎈"
            foreground: root.foreground
            hasCursor: root.miniCursorActive && root.miniCursor === "lyrics"
            tooltipText: "Open lyrics in Omasing · Ctrl+Shift+L"
            enabled: root.ytmusic && root.ytmusic.lyricsAvailable
            onClicked: root.openLyrics()
            onHovered: function(on) { if (on) root.setMiniCursor("lyrics") }
          }
        }

        Row {
          width: parent.width
          spacing: Style.space(8)
          visible: !root.lyricsInstallPromptVisible && root.ytmusic && root.ytmusic.hasPlayer

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: root.ytmusic && root.ytmusic.volume <= 0.001 ? "󰝟" : "󰕾"
            color: root.foreground
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.icon
          }

          PlaybackSlider {
            width: parent.width - Style.space(34)
            anchors.verticalCenter: parent.verticalCenter
            bar: root.bar
            minimum: 0
            maximum: 1
            step: 0.05
            sourceValue: root.ytmusic ? root.ytmusic.volume : 0
            contextKey: "volume"
            enabled: root.ytmusic && root.ytmusic.volumeSupported
            onCommitted: function(value) {
              if (root.ytmusic) root.ytmusic.setVolume(value)
            }
          }
        }

        PanelSeparator {
          foreground: root.foreground
          visible: !root.lyricsInstallPromptVisible
        }

        Row {
          width: parent.width
          spacing: Style.space(6)
          visible: !root.lyricsInstallPromptVisible

          Text {
            width: parent.width - openButton.width - Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            text: !root.ytmusic ? "Starting YouTube Music…"
              : (root.ytmusic.lastError !== "" ? root.ytmusic.lastError
              : (root.ytmusic.statusMessage !== "" ? root.ytmusic.statusMessage
              : (!root.ytmusic.accountConnected
                ? "Connect YouTube Music to browse your library"
                : (root.ytmusic.playing ? "Playing on this computer"
                  : "Ready when you press play"))))
            color: Qt.darker(root.foreground, 1.35)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }

          Button {
            id: openButton
            text: "Open"
            iconText: "󰏋"
            foreground: root.foreground
            hasCursor: root.miniCursorActive && root.miniCursor === "open"
            tooltipText: "Open full player · O"
            onClicked: root.openFullPanel()
            onHovered: function(on) { if (on) root.setMiniCursor("open") }
          }
        }

        LyricsInstallPrompt {
          width: parent.width
          visible: root.lyricsInstallPromptVisible
          service: root.ytmusic
          foreground: root.foreground
          surfaceKey: root.lyricsRequestKey
          cancelHasCursor: root.miniCursorActive && root.miniCursor === "prompt-cancel"
          confirmHasCursor: root.miniCursorActive && root.miniCursor === "prompt-confirm"
          onCanceled: root.dismissLyricsInstallPrompt()
        }
      }

      Column {
        id: miniShortcutHelp
        anchors.fill: parent
        visible: root.miniShortcutHelpVisible
        spacing: Style.space(7)

        Row {
          width: parent.width
          spacing: Style.space(6)
          Text {
            width: parent.width - miniShortcutHelpClose.width - parent.spacing
            anchors.verticalCenter: parent.verticalCenter
            text: "Keyboard shortcuts"
            color: root.foreground
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.subtitle
            font.bold: true
          }
          Button {
            id: miniShortcutHelpClose
            iconText: "󰅖"
            foreground: root.foreground
            focusable: true
            hasCursor: root.miniCursorActive && root.miniCursor === "help-close"
            onClicked: root.toggleMiniShortcutHelp()
          }
        }

        PanelSeparator { foreground: root.foreground }

        Repeater {
          model: root.miniShortcutRows
          delegate: Row {
            required property var modelData
            width: miniShortcutHelp.width
            spacing: Style.space(8)
            Text {
              width: Style.space(128)
              text: modelData.keys
              color: root.foreground
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.caption
              font.bold: true
            }
            Text {
              width: parent.width - Style.space(128) - parent.spacing
              text: modelData.action
              color: Qt.darker(root.foreground, 1.35)
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }
        }
      }
    }
  }

  Connections {
    target: root.ytmusic
    ignoreUnknownSignals: true
    function onLyricsPluginPromptRequested(surface, availability) {
      if (String(surface) !== root.lyricsRequestKey) return
      root.lyricsInstallPromptVisible = true
      root.popupOpen = true
    }
    function onLyricsPluginOpened(surface) {
      if (String(surface) !== root.lyricsRequestKey) return
      root.lyricsInstallPromptVisible = false
      root.close()
    }
  }
}
