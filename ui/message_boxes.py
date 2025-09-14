from PyQt5.Qt import QWIDGETSIZE_MAX
from PyQt5.QtCore import QEvent
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QMessageBox, QSpacerItem, QSizePolicy, QApplication, QTextEdit, QLabel

__author__ = 'Gary Hughes'


class ResizeableMessageBox(QMessageBox):

    def __init__(self, *args, **kwargs):
        super(ResizeableMessageBox, self).__init__(*args, **kwargs)
        self.setSizeGripEnabled(True)

        self.fixed_width_font = QFont()
        self.fixed_width_font.setFamily('Cascadia Mono, Source Code Pro, Consolas, Courier')
        self.fixed_width_font.setStyleHint(QFont.TypeWriter)

    def event(self, event: QEvent):
        if self.maximumWidth() != QWIDGETSIZE_MAX:
            text_edit = self.findChild(QTextEdit)
            if text_edit and text_edit.isVisible():
                # Detailed Text widget is visible - set our custom font and allow resizing of the QMessageBox.
                text_edit.document().setDefaultFont(self.fixed_width_font)
                text_edit.setMaximumSize(QWIDGETSIZE_MAX, QWIDGETSIZE_MAX)
                text_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                self.setMaximumSize(QWIDGETSIZE_MAX, QWIDGETSIZE_MAX)
                self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            else:
                # When Detailed Text widget is hidden, still allow the QMessageBox to be resized horizontally.
                self.setMaximumWidth(QWIDGETSIZE_MAX)
                self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

            # Set minimum width of Informative Text label. We're setting this on the label rather than the
            # QMessageBox itself so that the height of the QMessageBox adapts to the text.
            lbl_informative = self.findChild(QLabel, name='qt_msgbox_informativelabel')
            lbl_informative.setMinimumWidth(400)

        return super(ResizeableMessageBox, self).event(event)


def message_box_ok_cancel(text, informative_text, title=None, icon=QMessageBox.Critical, verb_pos=None, verb_neg=None,
                          allow_abort=False):
    msg_box = QMessageBox()
    msg_box.setText(f'<b>{text:s}</b>')
    if informative_text:
        msg_box.setInformativeText(informative_text)
    msg_box.setIcon(icon)
    if title:
        msg_box.setWindowTitle(title)
    else:
        msg_box.setWindowTitle(QApplication.instance().applicationName())

    msg_box.setDefaultButton(QMessageBox.Ok)

    if allow_abort:
        # Add an Abort button that can be triggered by pressing Escape or closing the dialog. We'll use this to return
        # a None result from the message box.
        msg_box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel | QMessageBox.Abort)
        msg_box.setEscapeButton(QMessageBox.Abort)
        msg_box.button(QMessageBox.Abort).setVisible(False)
    else:
        msg_box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)

    if verb_pos:
        msg_box.button(QMessageBox.Ok).setText(verb_pos)
    if verb_neg:
        msg_box.button(QMessageBox.Cancel).setText(verb_neg)

    horizontal_spacer = QSpacerItem(400, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
    layout = msg_box.layout()
    layout.addItem(horizontal_spacer, layout.rowCount(), 0, 1, layout.columnCount())
    response = msg_box.exec()

    if response == QMessageBox.Abort:
        return None
    return response


def message_box_ok(text, informative_text, title=None, icon=QMessageBox.Information):
    msg_box = QMessageBox()
    msg_box.setText(f'<b>{text:s}</b>')
    if informative_text:
        msg_box.setInformativeText(informative_text)
    msg_box.setIcon(icon)
    if title:
        msg_box.setWindowTitle(title)
    else:
        msg_box.setWindowTitle((QApplication.instance().applicationName()))
    msg_box.setStandardButtons(QMessageBox.Ok)
    msg_box.setDefaultButton(QMessageBox.Ok)
    horizontal_spacer = QSpacerItem(400, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
    layout = msg_box.layout()
    layout.addItem(horizontal_spacer, layout.rowCount(), 0, 1, layout.columnCount())
    return msg_box.exec_()


def message_box_yes_no(text, informative_text, title=None, icon=QMessageBox.Question):
    msg_box = QMessageBox()
    msg_box.setText(f'<b>{text:s}</b>')
    if informative_text:
        msg_box.setInformativeText(informative_text)
    msg_box.setIcon(icon)
    if title:
        msg_box.setWindowTitle(title)
    else:
        msg_box.setWindowTitle((QApplication.instance().applicationName()))
    msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    msg_box.setDefaultButton(QMessageBox.Yes)
    horizontal_spacer = QSpacerItem(400, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
    layout = msg_box.layout()
    layout.addItem(horizontal_spacer, layout.rowCount(), 0, 1, layout.columnCount())
    return msg_box.exec_()


def message_box_error(text, informative_text, title=None, detailed_text=None, icon=QMessageBox.Critical):
    msg_box = ResizeableMessageBox()
    msg_box.setText(f'<b>{text:s}</b>')
    if informative_text:
        msg_box.setInformativeText(informative_text)
    if detailed_text:
        msg_box.setDetailedText(detailed_text)
        button_copy = msg_box.addButton(' &Copy Details ', QMessageBox.ActionRole)
        button_copy.clicked.disconnect()
        button_copy.released.connect(lambda: QApplication.instance().clipboard().setText(detailed_text))
    msg_box.setIcon(icon)
    if title:
        msg_box.setWindowTitle(title)
    else:
        msg_box.setWindowTitle((QApplication.instance().applicationName()))
    msg_box.setStandardButtons(QMessageBox.Ok)
    msg_box.setDefaultButton(QMessageBox.Ok)
    return msg_box.exec_()
