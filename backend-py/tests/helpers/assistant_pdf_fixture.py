"""Small real text/image-only PDF fixtures without network or extra packages."""
import io

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject, NumberObject


def make_pdf(*pages, password=None):
    writer = PdfWriter()
    for lines in pages:
        page = writer.add_blank_page(width=1000, height=1000)
        if lines:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
            })
            stream = DecodedStreamObject()
            escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
            stream.set_data(
                ("BT /F1 10 Tf 20 950 Td 14 TL " +
                 " ".join(f"({line}) Tj T*" for line in escaped) + " ET").encode("latin1")
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
        else:
            image = DecodedStreamObject()
            image.update({
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
                NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            })
            image.set_data(b"\xff\xff\xff")
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(image)}),
            })
            stream = DecodedStreamObject()
            stream.set_data(b"q 100 0 0 100 0 0 cm /Im0 Do Q")
            page[NameObject("/Contents")] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
