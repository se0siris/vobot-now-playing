import sys
import logging

is_frozen = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')


def setup_logging(ignore_frozen=False):
    if not is_frozen or ignore_frozen:
        import coloredlogs

        coloredlogs.install(
            level=logging.DEBUG,
            fmt='%(asctime)s | %(module)-16s | %(levelname)-8s | %(message)s',
            field_styles={
                'asctime': {
                    'color': 'cyan'
                },
                'module': {
                    'color': 'magenta'
                },
                'levelname': {
                    'color': 'white',
                    'bold': True
                }
            },
            level_styles={
                'critical': {
                    'bold': True,
                    'color': 'red'
                },
                'debug': {
                    'color': 'green',
                    'faint': True
                },
                'error': {
                    'color': 'red'
                },
                'info': {
                    'color': 'blue'
                },
                'warning': {
                    'color': 'yellow'
                }
            }
        )


setup_logging()
logger = logging.getLogger()
logging.getLogger('PIL').setLevel(logging.FATAL)
# logger.setLevel(logging.INFO)
