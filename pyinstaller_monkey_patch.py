import xml.dom.minidom

import logging

from PyInstaller.utils.win32.winmanifest import _DEFAULT_MANIFEST_XML as manifest_xml

logger = logging.getLogger()


def _set_dpi_awareness():
    with xml.dom.minidom.parseString(manifest_xml) as manifest_dom:
        doc = manifest_dom.documentElement

        try:
            windows_settings = doc.getElementsByTagName('windowsSettings')[0]

            logger.info('Adding dpiAwareness setting to manifest')

            dpi_awareness = manifest_dom.createElement('dpiAwareness')
            dpi_awareness.setAttribute('xmlns', 'http://schemas.microsoft.com/SMI/2016/WindowsSettings')
            dpi_awareness_text = manifest_dom.createTextNode('system')
            dpi_awareness.appendChild(dpi_awareness_text)

            windows_settings.appendChild(dpi_awareness)
        except IndexError:
            pass

        output = manifest_dom.toprettyxml(indent='  ', encoding='UTF-8').decode('utf-8')
        return output


manifest = _set_dpi_awareness()
