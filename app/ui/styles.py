DARK_QSS = """
QWidget {
    background-color: #0f1115;
    color: #e8eaf0;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog {
    background-color: #0f1115;
}
#HeaderTitle {
    font-size: 18px;
    font-weight: 700;
    color: #ffffff;
}
#HeaderSub {
    font-size: 11px;
    color: #8a91a5;
}
#StatusDot {
    border-radius: 7px;
    background-color: #3a4152;
}
QFrame#SidePanel {
    background-color: #14171e;
    border: 1px solid #222733;
    border-radius: 14px;
}
/* v2.2.13：全局 QWidget 底色会在卡片标签后形成深色"补丁"（用户实拍反馈），
   面板内标签统一显式透明 */
QFrame#SidePanel QLabel {
    background: transparent;
}
QLabel#PanelTitle {
    font-size: 12px;
    font-weight: 700;
    color: #9aa3b8;
    letter-spacing: 1px;
}
QComboBox, QSpinBox {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 8px;
    padding: 6px 10px;
    color: #e8eaf0;
    min-height: 20px;
}
QComboBox:hover, QSpinBox:hover {
    border-color: #4f8cff;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    selection-background-color: #2b3a5e;
    color: #e8eaf0;
    outline: none;
}
QPushButton {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 8px;
    padding: 7px 14px;
    color: #dfe3ee;
}
QPushButton:hover {
    background-color: #232936;
    border-color: #3d4656;
}
QPushButton:disabled {
    color: #5a6172;
    background-color: #171b23;
}
QPushButton#PrimaryButton {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4f8cff, stop:1 #7b5cff);
    border: none;
    color: #ffffff;
    font-weight: 700;
    font-size: 14px;
    padding: 10px 18px;
    border-radius: 10px;
}
QPushButton#GhostButton {
    background-color: transparent;
    border: 1px solid #2a3040;
    color: #9aa3b8;
    padding: 6px 12px;
}
QPushButton#GhostButton:hover {
    background-color: #232936;
    color: #dfe3ee;
    border-color: #3d4656;
}
QPushButton#PrimaryButton:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #619aff, stop:1 #8d74ff);
}
QPushButton#StopButton {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ff5c7a, stop:1 #ff7b4f);
    border: none;
    color: #ffffff;
    font-weight: 700;
    font-size: 14px;
    padding: 10px 18px;
    border-radius: 10px;
}
QFrame#CaptionCard {
    background-color: #161a22;
    border: 1px solid #242a38;
    border-radius: 12px;
}
/* v2.2.5：聚焦态——最新一条字幕卡强调边框+微亮背景，一眼锁定"正在说" */
QFrame#CaptionCard#CaptionCardActive {
    background-color: #1a1f2b;
    border: 1px solid #4f8cff;
    border-radius: 12px;
}
/* v2.2.5：历史卡渐隐——降低文字对比度，视觉焦点留给最新句 */
QFrame#CaptionCard#CaptionCardOld QLabel#CaptionSource {
    color: #5a6172;
}
QFrame#CaptionCard#CaptionCardOld QLabel#CaptionTarget {
    color: #9aa3b8;
}
QFrame#CaptionCard#CaptionCardOld QLabel#CaptionMeta {
    color: #454c5e;
}
QLabel#CaptionSource {
    color: #8a91a5;
    font-size: 12px;
}
QLabel#CaptionTarget {
    color: #ffffff;
    font-size: 15px;
    font-weight: 600;
}
QLabel#CaptionMeta {
    color: #5a6172;
    font-size: 11px;
}
QLabel#EmptyHint {
    color: #5a6172;
    font-size: 14px;
    background: transparent;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #2c3345;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #3a4360;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QStatusBar {
    background-color: #12151c;
    color: #8a91a5;
    border-top: 1px solid #1e2330;
}
/* v2.2.5：关键提示横幅——琥珀色警示文字（错误时由代码切换为橙红），不挤状态行 */
QLabel#StatusAlert {
    font-size: 12px;
    font-weight: 600;
    background: rgba(251, 191, 36, 26);
    border: 1px solid rgba(251, 191, 36, 70);
    border-radius: 6px;
    padding: 3px 10px;
}
QProgressBar {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 6px;
    text-align: center;
    color: #e8eaf0;
    height: 16px;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4f8cff, stop:1 #7b5cff);
    border-radius: 5px;
}
QToolTip {
    background-color: #1b1f29;
    color: #e8eaf0;
    border: 1px solid #2a3040;
    padding: 4px;
}
#LevelBar {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 4px;
}
"""

OVERLAY_QSS = """
QLabel#OverlaySource {
    color: rgba(255, 255, 255, 150);
    font-size: 13px;
    background: transparent;
}
QLabel#OverlayTarget {
    color: #ffffff;
    font-size: 18px;
    font-weight: 700;
    background: transparent;
}
QLabel#OverlayStatus {
    color: rgba(255, 255, 255, 150);
    font-size: 11px;
    background: transparent;
}
QPushButton#OverlayClose {
    background: transparent;
    border: none;
    border-radius: 11px;
    color: rgba(255, 255, 255, 190);
    font-size: 12px;
    padding: 0px;
}
QPushButton#OverlayClose:hover {
    background: rgba(255, 255, 255, 46);
    color: #ffffff;
}
"""

SETTING_QSS = """
QDialog {
    background-color: #0f1115;
}
QListWidget#NavList {
    background-color: #12151c;
    border: none;
    border-right: 1px solid #1e2330;
    color: #c9cfdd;
    font-size: 13px;
    outline: none;
    padding-top: 8px;
}
QListWidget#NavList::item {
    color: #c9cfdd;
    padding-left: 18px;
    border-left: 3px solid transparent;
}
QListWidget#NavList::item:selected {
    background-color: #2b3a5e;
    color: #ffffff;
    border-left: 3px solid #4f8cff;
}
QListWidget#NavList::item:hover:!selected {
    background-color: #1a1f2a;
}
QWidget#SettingInner, QScrollArea#SettingScroll {
    background-color: #0f1115;
}
QLabel#SettingTitle {
    font-size: 15px;
    font-weight: 700;
    color: #ffffff;
    background: transparent;
}
QLabel#SettingDesc {
    font-size: 11px;
    color: #8a91a5;
    background: transparent;
}
QLabel#SettingGroup {
    font-size: 12px;
    font-weight: 700;
    color: #9aa3b8;
    letter-spacing: 1px;
    background: transparent;
}
QFrame#SettingSep {
    border: none;
    border-top: 1px solid #1e2330;
    background: transparent;
}
QCheckBox, QSlider, QSpinBox {
    background: transparent;
}
QCheckBox {
    color: #e8eaf0;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 1px solid #3d4656;
    background-color: #1b1f29;
}
QCheckBox::indicator:hover {
    border-color: #4f8cff;
}
QCheckBox::indicator:checked {
    border: 1px solid #4f8cff;
    background-color: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #ffffff, stop:0.42 #4f8cff, stop:0.62 #4f8cff, stop:1 #4f8cff);
}
QCheckBox::indicator:disabled {
    border-color: #2a3040;
    background-color: #171b23;
}
QSpinBox, QDoubleSpinBox {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 8px;
    padding: 4px 10px;
    color: #e8eaf0;
    min-height: 20px;
    selection-background-color: #2b3a5e;
}
QSpinBox:hover, QDoubleSpinBox:hover {
    border-color: #4f8cff;
}
QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #4f8cff;
}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 18px;
    border: none;
    background: transparent;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background: #232936;
}
QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: #5a6172;
    background-color: #171b23;
}
QSlider::groove:horizontal {
    height: 4px;
    background: #2a3040;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #4f8cff;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
    background: #e8eaf0;
}
QSlider::handle:horizontal:hover {
    background: #ffffff;
}
QPushButton#ColorPickButton {
    min-height: 26px;
    font-weight: 600;
    font-size: 12px;
}
QLabel#SavedHint {
    color: #34d399;
    font-size: 12px;
    padding: 5px 0 7px 0;
    background-color: #12151c;
    border-top: 1px solid #1e2330;
}
QLabel#AboutAppName {
    font-size: 20px;
    font-weight: 700;
    color: #ffffff;
    background: transparent;
}
QLabel#AboutVersion {
    font-size: 12px;
    color: #8a91a5;
    background: transparent;
}
QFrame#ActionBar {
    background-color: #12151c;
    border-top: 1px solid #1e2330;
}
QLabel#DirtyHint {
    font-size: 12px;
    color: #8a91a5;
    background: transparent;
}
QLineEdit#SearchBox {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 10px;
    padding: 4px 10px;
    color: #e8eaf0;
    font-size: 12px;
}
QLineEdit#SearchBox:focus {
    border-color: #4f8cff;
}
QWidget {
    background-color: #0f1115;
    color: #e8eaf0;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}
QComboBox {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 8px;
    padding: 6px 10px;
    color: #e8eaf0;
    min-height: 20px;
}
QComboBox:hover {
    border-color: #4f8cff;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    selection-background-color: #2b3a5e;
    color: #e8eaf0;
    outline: none;
}
QPushButton {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 8px;
    padding: 6px 14px;
    color: #dfe3ee;
}
QPushButton:hover {
    background-color: #232936;
    border-color: #3d4656;
}
QPushButton:disabled {
    color: #5a6172;
    background-color: #171b23;
}
QMessageBox, QToolTip {
    background-color: #161a22;
}
QMessageBox QLabel {
    color: #e8eaf0;
    background: transparent;
}
QMessageBox QPushButton {
    min-width: 72px;
}
"""
