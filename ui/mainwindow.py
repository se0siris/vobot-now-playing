import logging

from PyQt5.QtCore import QThread, QTimer, pyqtSlot
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication, QMainWindow

from ui.Ui_mainwindow import Ui_MainWindow

from ui.notifications import NotificationsWrapper

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow, Ui_MainWindow):

    def __init__(self):
        super(MainWindow, self).__init__()
        self.setupUi(self)
        self.setWindowTitle(f'{QApplication.applicationName()} - v{QApplication.applicationVersion()}')

        logger.debug('MainWindow initialized')

        QTimer.singleShot(0, self.setup_notifications)

    def setup_notifications(self):
        self.notifications_wrapper = NotificationsWrapper()
        self._notifications_thread = QThread()
        self.notifications_wrapper.moveToThread(self._notifications_thread)
        self._notifications_thread.started.connect(self.notifications_wrapper.start)
        self.notifications_wrapper.signal_thumb_bytes.connect(self.receive_thumb_bytes)
        self._notifications_thread.start()

    @pyqtSlot()
    def on_button_close_clicked(self):
        self.close()

    @pyqtSlot(bytes)
    def receive_thumb_bytes(self, thumb_bytes):
        logger.debug('Received thumbnail (%d Kb)', len(thumb_bytes) // 1024)

        # Load image.
        thumb_pixmap = QPixmap()
        thumb_pixmap.loadFromData(thumb_bytes)

        self.lbl_preview.setPixmap(thumb_pixmap)
