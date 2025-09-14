from PyQt5.QtWidgets import QApplication, QMainWindow

from ui.Ui_mainwindow import Ui_MainWindow


class MainWindow(QMainWindow, Ui_MainWindow):

    def __init__(self):
        super(MainWindow, self).__init__()
        self.setupUi(self)
        self.setWindowTitle(f'{QApplication.applicationName()} - v{QApplication.applicationVersion()}')
