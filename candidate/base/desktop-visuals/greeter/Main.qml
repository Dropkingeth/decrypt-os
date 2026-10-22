// Decrypt visual adaptation of pinned Omarchy Main.qml. MIT notice in LICENSE.
// Authentication remains SDDM's native local-account flow.
import QtQuick 2.0
import SddmComponents 2.0

Rectangle {
  id: root
  width: 1280
  height: 800
  color: "#060b13"
  property string currentUser: userModel.lastUser || config.Username
  property bool loginFailed: false
  property bool busy: false
  property bool compact: height < 650
  property int sessionIndex: {
    for (var i = 0; i < sessionModel.rowCount(); i++) {
      var name = (sessionModel.data(sessionModel.index(i, 0), Qt.DisplayRole) || "").toString()
      if (name.indexOf("uwsm") !== -1) return i
    }
    return sessionModel.lastIndex
  }
  function submit() {
    if (busy || !currentUser || password.text.length === 0) return
    busy = true
    sddm.login(currentUser, password.text, sessionIndex)
  }
  Image { anchors.fill: parent; source: "background.png"; fillMode: Image.PreserveAspectCrop }
  Rectangle { anchors.fill: parent; color: "#060b13"; opacity: 0.3 }
  Connections {
    target: sddm
    function onLoginFailed() {
      root.busy = false; root.loginFailed = true
      password.text = ""; password.forceActiveFocus()
    }
    function onLoginSucceeded() { root.busy = false; root.loginFailed = false; password.text = "" }
  }
  Column {
    width: Math.min(360, root.width - 64)
    anchors.centerIn: parent
    spacing: root.compact ? 10 : 18
    Row {
      anchors.horizontalCenter: parent.horizontalCenter
      spacing: 12
      Image { source: "decrypt-labs.svg"; width: 32; height: 36; fillMode: Image.PreserveAspectFit }
      Text { text: "DECRYPT OS"; font.family: "Noto Sans"; font.pixelSize: 19; font.letterSpacing: 3; color: "#eef4f6"; anchors.verticalCenter: parent.verticalCenter }
    }
    Item { width: 1; height: root.compact ? 0 : 12 }
    Text { text: "Welcome back"; font.family: "Noto Sans"; font.pixelSize: root.compact ? 26 : 32; color: "#eef4f6"; anchors.horizontalCenter: parent.horizontalCenter }
    Text {
      text: root.currentUser ? "Sign in as " + root.currentUser : "No local account selected"
      width: parent.width; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter
      font.family: "Noto Sans"; font.pixelSize: 15; color: "#b7c6d3"
    }
    Item { width: 1; height: 2 }
    Text { text: "Local password"; font.family: "Noto Sans"; font.pixelSize: 14; color: "#eef4f6" }
    Rectangle {
      width: parent.width; height: 54; radius: 6; color: "#060b13"
      border.width: password.activeFocus ? 2 : 1
      border.color: root.loginFailed ? "#ffb0a4" : password.activeFocus ? "#6fe3d6" : "#8496a8"
      TextInput {
        id: password
        anchors.fill: parent; anchors.margins: 15
        verticalAlignment: TextInput.AlignVCenter
        echoMode: TextInput.Password
        passwordCharacter: "•"
        font.family: "Noto Sans"; font.pixelSize: 18
        color: "#eef4f6"; selectionColor: "#1c3a4a"; selectedTextColor: "#eef4f6"
        enabled: !root.busy; focus: true
        Accessible.name: "Local password"
        Keys.onReturnPressed: root.submit()
        Keys.onEnterPressed: root.submit()
        KeyNavigation.tab: signIn
      }
    }
    Text {
      width: parent.width
      text: root.loginFailed ? "That password did not work. Try again." : "Use the password you created on this device."
      color: root.loginFailed ? "#ffb0a4" : "#b7c6d3"
      font.family: "Noto Sans"; font.pixelSize: 13; wrapMode: Text.Wrap
      Accessible.role: Accessible.StaticText
    }
    Rectangle {
      id: signIn
      width: parent.width; height: 48; radius: 6
      color: activeFocus ? "#8cebe0" : "#6fe3d6"
      opacity: root.busy ? 0.7 : 1
      border.width: activeFocus ? 2 : 0; border.color: "#f6fafb"
      activeFocusOnTab: true
      Accessible.role: Accessible.Button
      Accessible.name: root.busy ? "Signing in" : "Sign in"
      Text { anchors.centerIn: parent; text: root.busy ? "Signing in…" : "Sign in"; color: "#062a2b"; font.family: "Noto Sans"; font.pixelSize: 15; font.bold: true }
      MouseArea { anchors.fill: parent; enabled: !root.busy; onClicked: root.submit(); cursorShape: Qt.PointingHandCursor }
      Keys.onReturnPressed: root.submit()
      Keys.onEnterPressed: root.submit()
      Keys.onSpacePressed: root.submit()
      KeyNavigation.tab: password
      KeyNavigation.backtab: password
    }
  }
  Text {
    anchors.horizontalCenter: parent.horizontalCenter
    anchors.bottom: parent.bottom; anchors.bottomMargin: 28
    text: "Built on Omarchy"; font.family: "Noto Sans"; font.pixelSize: 12; color: "#b7c6d3"
  }
  Component.onCompleted: password.forceActiveFocus()
}
