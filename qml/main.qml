import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    visible: true
    width: 1180
    height: 800
    minimumWidth: 860
    minimumHeight: 560
    title: "CAN DBC Emulator" + (canController.dbcName ? " — " + canController.dbcName : "")

    Material.theme: Material.Dark
    Material.accent: Material.Orange
    Material.primary: Material.color(Material.Grey, Material.Shade900)

    FileDialog {
        id: dbcDialog
        title: "Select a DBC database"
        nameFilters: ["DBC files (*.dbc)", "All files (*)"]
        onAccepted: canController.loadDbc(selectedFile)
    }

    Dialog {
        id: vcanHelpDialog
        objectName: "vcanHelpDialog"
        title: "Manual setup needed"
        modal: true
        anchors.centerIn: parent
        width: Math.min(560, window.width - 80)
        standardButtons: Dialog.Close

        property string vcanName: "vcan0"

        contentItem: ColumnLayout {
            spacing: 12

            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "Creating a virtual CAN interface needs root privileges, which this app doesn't have (and shouldn't run with). Run this once in a terminal, then just pick the channel below and connect:"
            }

            TextArea {
                objectName: "vcanCommandText"
                Layout.fillWidth: true
                readOnly: true
                selectByMouse: true
                wrapMode: Text.WrapAnywhere
                font.family: "monospace"
                text: "sudo " + canController.vcanScriptPath + " " + vcanHelpDialog.vcanName
                background: Rectangle { color: Qt.darker(window.Material.background, 1.3); radius: 4 }
            }

            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                opacity: 0.7
                font.pixelSize: 11
                text: "Equivalent manual commands:\nsudo modprobe vcan\nsudo ip link add dev " + vcanHelpDialog.vcanName + " type vcan\nsudo ip link set up " + vcanHelpDialog.vcanName
            }

            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                opacity: 0.7
                font.pixelSize: 11
                text: "The interface stays up until reboot - you only need to do this once."
            }
        }
    }

    ListModel { id: logModel }

    function pushLog(text) {
        var timestamp = Qt.formatTime(new Date(), "hh:mm:ss")
        logModel.append({ text: "[" + timestamp + "] " + text })
        if (logModel.count > 400)
            logModel.remove(0)
        logView.positionViewAtEnd()
    }

    Connections {
        target: canController
        function onDbcError(message) { pushLog("ERROR: " + message) }
        function onConnectionError(message) { pushLog("ERROR: " + message) }
        function onLogMessage(message) { pushLog(message) }
    }

    header: ToolBar {
        Material.elevation: 2
        implicitHeight: toolbarColumn.implicitHeight + toolbarColumn.anchors.margins * 2

        ColumnLayout {
            id: toolbarColumn
            anchors.fill: parent
            anchors.margins: 8
            spacing: 6

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                Button {
                    text: "Load DBC…"
                    onClicked: dbcDialog.open()
                }
                Label {
                    Layout.fillWidth: true
                    elide: Text.ElideMiddle
                    text: canController.dbcName
                        ? (canController.dbcName + " — " + canController.messagesModel.rowCount() + " messages")
                        : "No DBC loaded"
                    opacity: canController.dbcName ? 1.0 : 0.6
                }
                Rectangle {
                    width: 12; height: 12; radius: 6
                    color: canController.connected ? "#4CAF50" : "#B0413E"
                }
                Label {
                    text: canController.connected ? "Connected" : "Disconnected"
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                Label { text: "Interface" }
                ComboBox {
                    id: ifaceTypeBox
                    objectName: "ifaceTypeBox"
                    model: ["socketcan", "virtual", "slcan"]
                    enabled: !canController.connected
                    Layout.preferredWidth: 120
                }

                Label { text: "Channel" }
                ComboBox {
                    id: channelBox
                    objectName: "channelBox"
                    editable: true
                    model: canController.availableInterfaces
                    enabled: !canController.connected
                    Layout.preferredWidth: 150
                }
                ToolButton {
                    text: "↻"
                    enabled: !canController.connected
                    ToolTip.text: "Refresh interface list"
                    ToolTip.visible: hovered
                    onClicked: canController.refreshInterfaces()
                }

                Button {
                    objectName: "createVcanButton"
                    text: "Create vcan"
                    visible: ifaceTypeBox.currentText === "socketcan"
                    enabled: !canController.connected
                    ToolTip.text: "Loads the vcan kernel module and brings up a virtual SocketCAN link (needs CAP_NET_ADMIN / root)"
                    ToolTip.visible: hovered
                    onClicked: {
                        var name = channelBox.editText.trim() || "vcan0"
                        channelBox.editText = name
                        if (!canController.createVcanInterface(name)) {
                            vcanHelpDialog.vcanName = name
                            vcanHelpDialog.open()
                        }
                    }
                }

                Label { text: "Bitrate" }
                ComboBox {
                    id: bitrateBox
                    editable: true
                    model: ["125000", "250000", "500000", "1000000"]
                    currentIndex: 2
                    enabled: !canController.connected
                    Layout.preferredWidth: 110
                    validator: IntValidator { bottom: 1; top: 10000000 }
                }

                Button {
                    text: "Apply Bitrate"
                    visible: ifaceTypeBox.currentText === "socketcan"
                    enabled: !canController.connected && channelBox.editText.length > 0
                    ToolTip.text: "Runs 'ip link set <iface> down/up' with this bitrate (needs CAP_NET_ADMIN / root)"
                    ToolTip.visible: hovered
                    onClicked: canController.applySocketcanBitrate(channelBox.editText, parseInt(bitrateBox.editText) || 500000)
                }

                Item { Layout.fillWidth: true }

                Button {
                    objectName: "connectButton"
                    text: canController.connected ? "Disconnect" : "Connect"
                    highlighted: !canController.connected
                    enabled: canController.connected || channelBox.editText.length > 0 || ifaceTypeBox.currentText === "virtual"
                    onClicked: {
                        if (canController.connected) {
                            canController.disconnectBus()
                        } else {
                            canController.connectBus(
                                ifaceTypeBox.currentText,
                                channelBox.editText,
                                parseInt(bitrateBox.editText) || 500000
                            )
                        }
                    }
                }
            }
        }
    }

    SplitView {
        anchors.fill: parent
        orientation: Qt.Vertical

        ListView {
            id: messageView
            objectName: "messageView"
            SplitView.fillHeight: true
            clip: true
            spacing: 6
            leftMargin: 8
            rightMargin: 8
            topMargin: 8
            bottomMargin: 8
            model: canController.messagesModel
            ScrollBar.vertical: ScrollBar {}

            delegate: Frame {
                id: msgCard
                required property string name
                required property int msgId
                required property string msgIdHex
                required property int dlc
                required property string comment
                required property int cycleTime
                required property bool transmitEnabled
                required property string rawHex
                required property var sigList
                required property bool extended

                property bool expanded: false

                width: ListView.view.width
                padding: 12

                background: Rectangle {
                    color: Qt.darker(window.Material.background, 1.25)
                    radius: 6
                    border.color: msgCard.transmitEnabled ? window.Material.accent : "#3a3a3a"
                    border.width: msgCard.transmitEnabled ? 2 : 1
                }

                ColumnLayout {
                    width: parent.width
                    spacing: 8

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 10

                        ToolButton {
                            objectName: "expandButton"
                            implicitWidth: 28
                            text: msgCard.expanded ? "▾" : "▸"
                            onClicked: msgCard.expanded = !msgCard.expanded
                        }

                        ColumnLayout {
                            spacing: 0
                            Label { text: msgCard.name; font.bold: true; font.pixelSize: 15 }
                            Label {
                                text: msgCard.msgIdHex + (msgCard.extended ? " (ext)" : "")
                                    + " · DLC " + msgCard.dlc
                                    + (msgCard.comment ? " · " + msgCard.comment : "")
                                opacity: 0.6
                                font.pixelSize: 11
                            }
                        }

                        Item { Layout.fillWidth: true }

                        Label {
                            text: msgCard.rawHex
                            font.family: "monospace"
                            color: window.Material.accent
                        }

                        Label { text: "every" }
                        SpinBox {
                            from: 5
                            to: 60000
                            stepSize: 5
                            value: msgCard.cycleTime
                            editable: true
                            Layout.preferredWidth: 150
                            onValueModified: canController.setCycleTime(msgCard.msgId, value)
                        }
                        Label { text: "ms" }

                        Button {
                            text: "Send once"
                            enabled: canController.connected
                            onClicked: canController.sendOnce(msgCard.msgId)
                        }

                        Switch {
                            objectName: "txSwitch"
                            text: "TX"
                            checked: msgCard.transmitEnabled
                            enabled: canController.connected
                            onToggled: canController.setTransmitEnabled(msgCard.msgId, checked)
                        }
                    }

                    Flow {
                        Layout.fillWidth: true
                        visible: msgCard.expanded
                        spacing: 16

                        Repeater {
                            model: msgCard.sigList

                            delegate: ColumnLayout {
                                width: 250
                                spacing: 2

                                property var sig: modelData
                                property real curValue: sig.initial

                                RowLayout {
                                    Layout.fillWidth: true
                                    Label {
                                        Layout.fillWidth: true
                                        text: sig.name
                                        font.bold: true
                                        elide: Text.ElideRight
                                    }
                                    Label {
                                        objectName: "sigValueLabel"
                                        text: curValue.toFixed(sig.decimals) + (sig.unit ? (" " + sig.unit) : "")
                                        color: window.Material.accent
                                    }
                                }

                                Slider {
                                    objectName: "sigSlider"
                                    Layout.fillWidth: true
                                    visible: sig.choices.length === 0
                                    from: sig.minimum
                                    to: sig.maximum
                                    stepSize: sig.step > 0 ? sig.step : 0
                                    value: sig.initial
                                    onMoved: {
                                        curValue = value
                                        canController.setSignalValue(msgCard.msgId, sig.name, value)
                                    }
                                }

                                ComboBox {
                                    Layout.fillWidth: true
                                    visible: sig.choices.length > 0
                                    model: sig.choices
                                    textRole: "name"
                                    valueRole: "value"
                                    Component.onCompleted: {
                                        for (var i = 0; i < sig.choices.length; i++) {
                                            if (sig.choices[i].value === sig.initial) {
                                                currentIndex = i
                                                break
                                            }
                                        }
                                    }
                                    onActivated: {
                                        curValue = currentValue
                                        canController.setSignalValue(msgCard.msgId, sig.name, currentValue)
                                    }
                                }

                                Label {
                                    Layout.fillWidth: true
                                    visible: sig.comment.length > 0
                                    text: sig.comment
                                    font.pixelSize: 10
                                    opacity: 0.6
                                    wrapMode: Text.WordWrap
                                }
                            }
                        }
                    }
                }
            }
        }

        Frame {
            SplitView.preferredHeight: 160
            SplitView.minimumHeight: 80

            ColumnLayout {
                anchors.fill: parent
                spacing: 4

                Label { text: "Log"; font.bold: true }

                ListView {
                    id: logView
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    model: logModel
                    delegate: Label {
                        text: model.text
                        font.family: "monospace"
                        font.pixelSize: 11
                    }
                }
            }
        }
    }
}
