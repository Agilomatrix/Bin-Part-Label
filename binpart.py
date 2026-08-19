import streamlit as st
import pandas as pd
import os
from reportlab.lib.pagesizes import landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Spacer, Paragraph, PageBreak, Image
from reportlab.lib.units import cm, inch
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.utils import ImageReader
from io import BytesIO
import subprocess
import sys
import re
import tempfile

# Define sticker dimensions
STICKER_WIDTH = 10 * cm
STICKER_HEIGHT = 15 * cm
STICKER_PAGESIZE = (STICKER_WIDTH, STICKER_HEIGHT)

# Define content box dimensions
CONTENT_BOX_WIDTH = 10 * cm  # Same width as page
CONTENT_BOX_HEIGHT = 7.2 * cm  # Half the page height

# Columns used for the manual-entry / mastersheet-template workflow
TEMPLATE_COLUMNS = [
    'Part No', 'Description', 'Location', 'Qty/Bin', 'Qty/Veh', 'Model', 'Store Location',
    'Station No', 'Rack', 'Rack No (1st digit)', 'Rack No (2nd digit)', 'Level', 'Cell',
    'Station Name', 'ABB ZONE', 'ABB LOCATION', 'ABB FLOOR', 'ABB RACK NO', 'ABB LEVEL IN RACK'
]

# Check for PIL and install if needed
try:
    from PIL import Image as PILImage
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    st.write("PIL not available. Installing...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pillow'])
    from PIL import Image as PILImage
    PIL_AVAILABLE = True

# Check for QR code library and install if needed
try:
    import qrcode
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False
    st.write("qrcode not available. Installing...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'qrcode'])
    import qrcode
    QR_AVAILABLE = True

# Check for openpyxl (needed to write the downloadable template)
try:
    import openpyxl  # noqa: F401
except ImportError:
    st.write("openpyxl not available. Installing...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'openpyxl'])
    import openpyxl  # noqa: F401

# Define paragraph styles
bold_style = ParagraphStyle(name='Bold', fontName='Helvetica-Bold', fontSize=16, alignment=TA_CENTER, leading=14)
desc_style = ParagraphStyle(name='Description', fontName='Helvetica', fontSize=11, alignment=TA_CENTER, leading=12)
qty_style = ParagraphStyle(name='Quantity', fontName='Helvetica', fontSize=11, alignment=TA_CENTER, leading=12)


def find_model_column(df_columns):
    """
    Find the "Model" column for a client's parts file. Generic by default
    (works for any product line), but still recognizes bus/vehicle-style
    headers for clients migrating from older sheets.
    """
    cols = [str(col).upper() for col in df_columns]

    # Priority order for model column detection
    patterns = [
        # Exact matches (highest priority) - generic first
        lambda col: col == 'MODEL',
        lambda col: col == 'PRODUCT_MODEL',
        lambda col: col == 'PRODUCTMODEL',
        lambda col: col == 'PRODUCT MODEL',
        lambda col: col == 'ITEM_MODEL',
        lambda col: col == 'ITEM MODEL',
        # Legacy / vehicle-style exact matches (kept for backward compatibility)
        lambda col: col == 'BUS_MODEL',
        lambda col: col == 'BUSMODEL',
        lambda col: col == 'BUS MODEL',
        lambda col: col == 'VEHICLE_MODEL',
        lambda col: col == 'VEHICLEMODEL',
        lambda col: col == 'VEHICLE MODEL',
        lambda col: col == 'BUS_TYPE',
        lambda col: col == 'BUSTYPE',
        lambda col: col == 'BUS TYPE',
        lambda col: col == 'VEHICLE_TYPE',
        lambda col: col == 'VEHICLETYPE',
        lambda col: col == 'VEHICLE TYPE',
        # Partial matches (lower priority)
        lambda col: 'MODEL' in col,
        lambda col: 'BUS' in col and 'TYPE' in col,
        lambda col: 'VEHICLE' in col and 'TYPE' in col,
        lambda col: 'BUS' in col,
        lambda col: 'VEHICLE' in col,
    ]

    for pattern in patterns:
        for i, col in enumerate(cols):
            if pattern(col):
                return df_columns[i]  # Return original column name

    return None


def normalize_model_token(value):
    """
    Normalize a raw model value into a clean label.
    Handles things like '9m', '9 M', '9' -> '9M'.
    Anything that isn't a bare number or number+M is kept as-is (uppercased),
    so non-numeric model names (e.g. 'LOW FLOOR', 'TYPE-A', 'STD') still work.
    """
    if value is None:
        return None
    val = str(value).strip()
    if not val or val.lower() in ['nan', 'none', 'null']:
        return None
    val_upper = val.upper()

    m = re.search(r'^(\d+)\s*M$', val_upper)
    if m:
        return f"{m.group(1)}M"

    m = re.match(r'^(\d+)$', val_upper)
    if m:
        return f"{m.group(1)}M"

    m = re.search(r'(\d+)\s*M\b', val_upper)
    if m:
        return f"{m.group(1)}M"

    return val_upper


def get_unique_models(df, model_col, qty_veh_col):
    """
    Scan the whole file and collect the distinct model labels that
    actually appear in it (no hardcoded 7M/9M/12M) - so this adapts to
    whatever product line/fleet a given client uses.
    """
    models = set()

    if model_col and model_col in df.columns:
        for v in df[model_col].dropna():
            token = normalize_model_token(v)
            if token:
                models.add(token)

    # Also pick up model:qty pairs embedded directly in the qty/veh column,
    # e.g. "9M:2", "12M-3"
    if qty_veh_col and qty_veh_col in df.columns:
        for v in df[qty_veh_col].dropna():
            val_upper = str(v).upper()
            for model, _qty in re.findall(r'(\d+M)[:\-\s]*(\d+)', val_upper):
                models.add(model)

    def sort_key(model):
        m = re.match(r'^(\d+)M$', model)
        if m:
            return (0, int(m.group(1)), model)
        return (1, 0, model)

    return sorted(models, key=sort_key)


def detect_model_qty_map(row, qty_veh_col, model_col, all_models):
    """
    For a single row, figure out the quantity that belongs to each of the
    models detected across the whole file (all_models). Returns a dict
    {model_label: qty_string}. Models with no data for this row map to ''.
    """
    result = {model: '' for model in all_models}
    if not all_models:
        return result

    # Get quantity value for this row
    qty_veh = ""
    if qty_veh_col and qty_veh_col in row and pd.notna(row[qty_veh_col]):
        qty_veh_raw = row[qty_veh_col]
        if pd.notna(qty_veh_raw):
            if isinstance(qty_veh_raw, float) and qty_veh_raw.is_integer():
                qty_veh = str(int(qty_veh_raw))
            else:
                qty_veh = str(qty_veh_raw).strip()

    if not qty_veh:
        return result

    # Method 1: qty/veh field itself contains model:qty pairs, e.g. "9M:2"
    qty_upper = qty_veh.upper()
    matches = re.findall(r'(\d+M)[:\-\s]*(\d+)', qty_upper)
    if matches:
        for model, quantity in matches:
            if model in result:
                result[model] = quantity
        return result

    # Method 2: dedicated model column tells us which model this row is
    if model_col and model_col in row and pd.notna(row[model_col]):
        token = normalize_model_token(row[model_col])
        if token and token in result:
            result[token] = qty_veh
            return result

    # Method 3: search any column's text for one of the known model labels
    for col in row.index:
        if pd.notna(row[col]):
            value_str = str(row[col]).upper()
            for model in all_models:
                if re.search(r'\b' + re.escape(model) + r'\b', value_str):
                    result[model] = qty_veh
                    return result

    return result


def generate_qr_code(data_string):
    """
    Generate a QR code from the given data string
    """
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )

        qr.add_data(data_string)
        qr.make(fit=True)

        qr_img = qr.make_image(fill_color="black", back_color="white")

        img_buffer = BytesIO()
        qr_img.save(img_buffer, format='PNG')
        img_buffer.seek(0)

        return Image(img_buffer, width=2.5*cm, height=2.5*cm)
    except Exception as e:
        st.error(f"Error generating QR code: {e}")
        import traceback
        traceback.print_exc()
        return None


def parse_location_string(location_str):
    """Parse a location string into components for table display"""
    location_parts = [''] * 7

    if not location_str or not isinstance(location_str, str):
        return location_parts

    location_str = location_str.strip()
    if location_str.lower() in ['nan', 'none', 'null']:
        return location_parts

    import re
    pattern = r'([^_\s]+)'
    matches = re.findall(pattern, location_str)

    for i, match in enumerate(matches[:7]):
        if match.lower() not in ['nan', 'none', 'null']:
            location_parts[i] = match

    return location_parts


def extract_location_data_from_excel(row_data):
    """Extract location data from Excel row for Line Location"""
    available_cols = list(row_data.index) if hasattr(row_data, 'index') else []

    def find_column_value(possible_names, default=''):
        for name in possible_names:
            if name in row_data:
                val = row_data[name]
                return str(val) if pd.notna(val) and str(val).lower() != 'nan' else default
            for col in available_cols:
                if isinstance(col, str) and col.upper() == name.upper():
                    val = row_data[col]
                    return str(val) if pd.notna(val) and str(val).lower() != 'nan' else default
        return default

    model = find_column_value(['Model', 'MODEL', 'Bus Model', 'Bus model', 'BUS MODEL', 'BUSMODEL', 'Bus_Model'])
    station_no = find_column_value(['Station No', 'Station no', 'STATION NO', 'STATIONNO', 'Station_No'])
    rack = find_column_value(['Rack', 'RACK', 'rack'])
    rack_no_1st = find_column_value(['Rack No (1st digit)', 'RACK NO (1st digit)', 'Rack_No_1st', 'RACK_NO_1ST'])
    rack_no_2nd = find_column_value(['Rack No (2nd digit)', 'RACK NO (2nd digit)', 'Rack_No_2nd', 'RACK_NO_2ND'])
    level = find_column_value(['Level', 'LEVEL', 'level'])
    cell = find_column_value(['Cell', 'CELL', 'cell'])

    return [model, station_no, rack, rack_no_1st, rack_no_2nd, level, cell]


def extract_store_location_data_from_excel(row_data):
    """Extract store location data from Excel row for Store Location"""
    def get_clean_value(possible_names, default=''):
        for name in possible_names:
            if name in row_data:
                val = row_data[name]
                if pd.notna(val) and str(val).lower() not in ['nan', 'none', 'null', '']:
                    return str(val).strip()
            for col in row_data.index:
                if isinstance(col, str) and col.upper() == name.upper():
                    val = row_data[col]
                    if pd.notna(val) and str(val).lower() not in ['nan', 'none', 'null', '']:
                        return str(val).strip()
        return default

    station_name = get_clean_value(['Station Name', 'STATION NAME', 'Station_Name', 'STATIONNAME'], '')
    store_location = get_clean_value(['Store Location', 'STORE LOCATION', 'Store_Location', 'STORELOCATION'], '')
    zone = get_clean_value(['ABB ZONE', 'ABB_ZONE', 'ABBZONE'], '')
    location = get_clean_value(['ABB LOCATION', 'ABB_LOCATION', 'ABBLOCATION'], '')
    floor = get_clean_value(['ABB FLOOR', 'ABB_FLOOR', 'ABBFLOOR'], '')
    rack_no = get_clean_value(['ABB RACK NO', 'ABB_RACK_NO', 'ABBRACKNO'], '')
    level_in_rack = get_clean_value(['ABB LEVEL IN RACK', 'ABB_LEVEL_IN_RACK', 'ABBLEVELINRACK'], '')

    return [station_name, store_location, zone, location, floor, rack_no, level_in_rack]


def create_blank_mastersheet_bytes():
    """
    Build a downloadable .xlsx template with every column the label
    generator understands, pre-filled with one example row so users know
    the expected format. Used for bulk uploads.
    """
    example_row = {
        'Part No': 'ABC123',
        'Description': 'Engine Filter',
        'Location': 'A1_B2_C3',
        'Qty/Bin': 5,
        'Qty/Veh': 2,
        'Model': '9M',
        'Store Location': 'Main Store',
        'Station No': '',
        'Rack': '',
        'Rack No (1st digit)': '',
        'Rack No (2nd digit)': '',
        'Level': '',
        'Cell': '',
        'Station Name': '',
        'ABB ZONE': '',
        'ABB LOCATION': '',
        'ABB FLOOR': '',
        'ABB RACK NO': '',
        'ABB LEVEL IN RACK': ''
    }
    template_df = pd.DataFrame([example_row], columns=TEMPLATE_COLUMNS)

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        template_df.to_excel(writer, index=False, sheet_name='Labels')
    buffer.seek(0)
    return buffer.getvalue()


def generate_sticker_labels_core(df, output_pdf_path, status_callback=None, include_model_box=True):
    """
    Core sticker-generation logic. Accepts an already-loaded DataFrame, so
    it can be reused both for bulk file uploads and for a small DataFrame
    built from manual entry.

    include_model_box: True to print the model box, False for clients
    that don't need it at all. The model labels themselves (e.g. 7M, 9M,
    12M, Type-A, or whatever a given client's product line uses) are
    always detected directly from the data - nothing is hardcoded.
    """
    def log(msg):
        if status_callback:
            status_callback(msg)
        else:
            st.write(msg)

    # Create a function to draw the border box around content
    def draw_border(canvas, doc):
        canvas.saveState()
        x_offset = (STICKER_WIDTH - CONTENT_BOX_WIDTH) / 2
        y_offset = STICKER_HEIGHT - CONTENT_BOX_HEIGHT - 0.2*cm
        canvas.setStrokeColor(colors.Color(0, 0, 0, alpha=0.95))
        canvas.setLineWidth(1.8)
        canvas.rect(
            x_offset + doc.leftMargin,
            y_offset,
            CONTENT_BOX_WIDTH - 0.2*cm,
            CONTENT_BOX_HEIGHT
        )
        canvas.restoreState()

    log(f"Preparing {len(df)} label(s)")

    # Identify columns (case-insensitive)
    df = df.copy()
    df.columns = [col.upper() if isinstance(col, str) else col for col in df.columns]
    cols = df.columns.tolist()

    part_no_col = next((col for col in cols if 'PART' in col and ('NO' in col or 'NUM' in col or '#' in col)),
                   next((col for col in cols if col in ['PARTNO', 'PART']), cols[0]))

    desc_col = next((col for col in cols if 'DESC' in col),
                   next((col for col in cols if 'NAME' in col), cols[1] if len(cols) > 1 else part_no_col))

    qty_bin_col = next((col for col in cols if 'QTY/BIN' in col or 'QTY_BIN' in col or 'QTYBIN' in col),
                  next((col for col in cols if 'QTY' in col and 'BIN' in col), None))

    if not qty_bin_col:
        qty_bin_col = next((col for col in cols if 'QTY' in col),
                      next((col for col in cols if 'QUANTITY' in col), None))

    loc_col = next((col for col in cols if 'LOC' in col or 'POS' in col or 'LOCATION' in col),
                   cols[2] if len(cols) > 2 else desc_col)

    qty_veh_col = next((col for col in cols if any(term in col for term in ['QTY/VEH', 'QTY_VEH', 'QTY PER VEH', 'QTYVEH', 'QTYPERCAR', 'QTYCAR', 'QTY/CAR'])), None)

    store_loc_col = next((col for col in cols if 'STORE' in col and 'LOC' in col),
                      next((col for col in cols if 'STORELOCATION' in col), None))

    # NOTE: must detect against the already-uppercased column names (cols),
    # not the original-case ones - df's own columns were just upper-cased
    # above, so a name mismatch here would make every row lookup silently fail.
    model_col = find_model_column(cols)

    log(f"Using columns: Part No: {part_no_col}, Description: {desc_col}, Location: {loc_col}, Qty/Bin: {qty_bin_col}")
    if qty_veh_col:
        log(f"Qty/Veh Column: {qty_veh_col}")
    if store_loc_col:
        log(f"Store Location Column: {store_loc_col}")
    if model_col:
        log(f"Model Column: {model_col}")

    # ---- Resolve the model box: which models exist in this file? ----
    all_models = []
    render_model_box = False
    if include_model_box:
        all_models = get_unique_models(df, model_col, qty_veh_col)
        if all_models:
            render_model_box = True
            log(f"Model Box: Include -> detected models: {', '.join(all_models)}")
        else:
            log("Model Box: Include was selected, but no model data was found, "
                "so no box will be printed.")
    else:
        log("Model Box: Exclude -> no model box will be printed")

    # Create document with minimal margins
    doc = SimpleDocTemplate(output_pdf_path, pagesize=STICKER_PAGESIZE,
                          topMargin=0.2*cm,
                          bottomMargin=(STICKER_HEIGHT - CONTENT_BOX_HEIGHT - 0.2*cm),
                          leftMargin=0.1*cm, rightMargin=0.1*cm)

    content_width = CONTENT_BOX_WIDTH - 0.2*cm
    all_elements = []

    # Sorting by rack columns if present
    rack_col = next((col for col in df.columns if col.strip().lower() == 'rack'), None)
    rack_no_1st_col = next((col for col in df.columns if '1st' in col.lower()), None)
    rack_no_2nd_col = next((col for col in df.columns if '2nd' in col.lower()), None)

    if rack_col and rack_no_1st_col and rack_no_2nd_col:
        df[rack_no_1st_col] = pd.to_numeric(df[rack_no_1st_col], errors='coerce')
        df[rack_no_2nd_col] = pd.to_numeric(df[rack_no_2nd_col], errors='coerce')

        df.sort_values(
            by=[rack_col, rack_no_1st_col, rack_no_2nd_col],
            ascending=[False, False, False],
            inplace=True
        )
    else:
        log("⚠️ Sorting skipped: could not find all rack-related columns.")

    # Process each row as a single sticker
    total_rows = len(df)
    for index, row in df.iterrows():
        log(f"Creating sticker {index+1} of {total_rows} ({int((index+1)/total_rows*100)}%)")

        elements = []

        # Extract data
        part_no = str(row[part_no_col])
        desc = str(row[desc_col])

        qty_bin = ""
        if qty_bin_col and qty_bin_col in row and pd.notna(row[qty_bin_col]):
            qty_bin = str(row[qty_bin_col])

        qty_veh = ""
        if qty_veh_col and qty_veh_col in row and pd.notna(row[qty_veh_col]):
            qty_veh = str(row[qty_veh_col])

        location_str = str(row[loc_col]) if loc_col and loc_col in row else ""
        store_location = str(row[store_loc_col]) if store_loc_col and store_loc_col in row else ""

        # QR code payload - Part No, Qty, Delivery (line) Location, Storage Location
        qr_data = f"Part No: {part_no}\nDescription: {desc}\nDelivery Location: {location_str}\n"
        qr_data += f"Storage Location: {store_location}\nQTY/VEH: {qty_veh}\nQTY/BIN: {qty_bin}"

        qr_image = generate_qr_code(qr_data)
        if status_callback and qr_image:
            status_callback(f"QR code generated for part: {part_no}")

        # Define row heights
        header_row_height = 0.9*cm
        desc_row_height = 1.0*cm
        qty_row_height = 0.5*cm
        location_row_height = 0.5*cm

        # Main table data
        main_table_data = [
            ["Part No", Paragraph(f"{part_no}", bold_style)],
            ["Description", Paragraph(desc[:47] + "..." if len(desc) > 50 else desc, desc_style)],
            ["Qty/Bin", Paragraph(str(qty_bin), qty_style)]
        ]

        main_table = Table(main_table_data,
                         colWidths=[content_width/3, content_width*2/3],
                         rowHeights=[header_row_height, desc_row_height, qty_row_height])

        main_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 1.2, colors.Color(0, 0, 0, alpha=0.95)),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (0, -1), 11),
        ]))

        elements.append(main_table)

        # Store Location section
        store_loc_label = Paragraph("Store Location", ParagraphStyle(
        name='StoreLoc', fontName='Helvetica-Bold', fontSize=11, alignment=TA_CENTER
        ))
        inner_table_width = content_width * 2 / 3

        col_proportions = [1.8, 2.4, 0.7, 0.7, 0.7, 0.7, 0.9]
        total_proportion = sum(col_proportions)

        inner_col_widths = [w * inner_table_width / total_proportion for w in col_proportions]

        store_loc_values = extract_store_location_data_from_excel(row)

        store_loc_inner_table = Table(
            [store_loc_values],
            colWidths=inner_col_widths,
            rowHeights=[location_row_height]
        )
        store_loc_inner_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 1.2, colors.Color(0, 0, 0, alpha=0.95)),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
        ]))
        store_loc_table = Table(
            [[store_loc_label, store_loc_inner_table]],
            colWidths=[content_width/3, inner_table_width],
            rowHeights=[location_row_height]
        )
        store_loc_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 1.2, colors.Color(0, 0, 0, alpha=0.95)),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        elements.append(store_loc_table)

        # Line Location section
        line_loc_label = Paragraph("Line Location", ParagraphStyle(
            name='LineLoc', fontName='Helvetica-Bold', fontSize=11, alignment=TA_CENTER
        ))
        location_parts = extract_location_data_from_excel(row)
        location_parts = [
            str(int(float(val))) if isinstance(val, str) and re.match(r'^\d+\.0$', val) else val
            for val in location_parts
        ]
        line_loc_inner_table = Table(
            [location_parts],
            colWidths=inner_col_widths,
            rowHeights=[location_row_height]
        )
        line_loc_inner_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 1.2, colors.Color(0, 0, 0, alpha=0.95)),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9)
        ]))
        line_loc_table = Table(
            [[line_loc_label, line_loc_inner_table]],
            colWidths=[content_width/3, inner_table_width],
            rowHeights=[location_row_height]
        )
        line_loc_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 1.2, colors.Color(0, 0, 0, alpha=0.95)),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        elements.append(line_loc_table)
        elements.append(Spacer(1, 0.3*cm))

        # ---- Bottom section: Model box (optional) + QR code ----
        qr_width = 2.5*cm
        qr_height = 2.5*cm

        if qr_image:
            qr_table = Table(
                [[qr_image]],
                colWidths=[qr_width],
                rowHeights=[qr_height]
            )
        else:
            qr_table = Table(
                [[Paragraph("QR", ParagraphStyle(
                    name='QRPlaceholder', fontName='Helvetica-Bold', fontSize=12, alignment=TA_CENTER
                ))]],
                colWidths=[qr_width],
                rowHeights=[qr_height]
            )

        qr_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))

        if render_model_box:
            model_quantities = detect_model_qty_map(row, qty_veh_col, model_col, all_models)

            n_models = len(all_models)
            model_row_height = 1.5*cm

            # Reserve roughly the same footprint as before for the box area,
            # but size each column to fit however many models this file has.
            reserved_area_width = content_width - qr_width - 1.6*cm  # minus spacers
            model_box_width = reserved_area_width / n_models
            model_box_width = max(0.65*cm, min(1.4*cm, model_box_width))

            header_row = list(all_models)
            value_row = [
                Paragraph(f"<b>{model_quantities[model]}</b>", ParagraphStyle(
                    name=f'BoldModel_{model}', fontName='Helvetica-Bold', fontSize=9, alignment=TA_CENTER
                )) if model_quantities[model] else ""
                for model in all_models
            ]

            model_table = Table(
                [header_row, value_row],
                colWidths=[model_box_width] * n_models,
                rowHeights=[model_row_height/2, model_row_height/2]
            )

            model_table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 1.2, colors.Color(0, 0, 0, alpha=0.95)),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 8 if n_models > 4 else 9),
                ('FONTSIZE', (0, 1), (-1, 1), 9),
            ]))

            model_table_width = model_box_width * n_models
            right_margin = 0.3*cm
            middle_spacer_width = max(0.3*cm, content_width - model_table_width - qr_width - right_margin)

            # Model box stays on the left; QR code is pushed to the right edge
            bottom_row = Table(
                [[model_table, "", qr_table, ""]],
                colWidths=[model_table_width, middle_spacer_width, qr_width, right_margin],
                rowHeights=[qr_height]
            )
        else:
            # No model box: push the QR code to the right edge of the label
            right_margin = 0.3*cm
            left_spacer_width = max(0.3*cm, content_width - qr_width - right_margin)
            bottom_row = Table(
                [["", qr_table, ""]],
                colWidths=[left_spacer_width, qr_width, right_margin],
                rowHeights=[qr_height]
            )

        bottom_row.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))

        elements.append(bottom_row)

        all_elements.extend(elements)

        if index < len(df) - 1:
            all_elements.append(PageBreak())

    # Build the document
    try:
        doc.build(all_elements, onFirstPage=draw_border, onLaterPages=draw_border)
        log(f"PDF generated successfully: {output_pdf_path}")
        return output_pdf_path
    except Exception as e:
        log(f"Error building PDF: {e}")
        if not status_callback:
            import traceback
            traceback.print_exc()
        return None


def generate_sticker_labels(excel_file_path, output_pdf_path, status_callback=None, include_model_box=True):
    """
    Generate sticker labels with QR code from an Excel/CSV file on disk.
    Loads the file into a DataFrame, then hands off to the shared core
    generator (generate_sticker_labels_core) used by both bulk upload and
    manual entry.
    """
    def log(msg):
        if status_callback:
            status_callback(msg)
        else:
            st.write(msg)

    log(f"Processing file: {excel_file_path}")

    # Load the Excel data
    try:
        if excel_file_path.lower().endswith('.csv'):
            df = pd.read_csv(excel_file_path)
        else:
            try:
                df = pd.read_excel(excel_file_path)
            except Exception:
                try:
                    df = pd.read_excel(excel_file_path, engine='openpyxl')
                except Exception:
                    df = pd.read_csv(excel_file_path, encoding='latin1')

        log(f"Successfully read file with {len(df)} rows")
        log(f"Columns found: {df.columns.tolist()}")
    except Exception as e:
        log(f"Error reading file: {e}")
        return None

    return generate_sticker_labels_core(df, output_pdf_path, status_callback=status_callback,
                                         include_model_box=include_model_box)


def main():
    """Main Streamlit application"""
    st.set_page_config(page_title="Bin Label Generator", page_icon="🏷️", layout="wide")

    st.title("🏷️ Bin Label Generator")
    st.markdown(
        "<p style='font-size:18px; font-style:italic; margin-top:-10px; text-align:left;'>"
        "Designed and Developed by Agilomatrix</p>",
        unsafe_allow_html=True
    )

    st.markdown("---")

    # Sidebar for configuration
    st.sidebar.header("Configuration")

    st.sidebar.subheader("Model Box")
    model_choice = st.sidebar.radio(
        "Print the model box on the labels?",
        options=["Include", "Exclude"],
        index=0,
        help=(
            "Include: the model box is printed, with model labels (e.g. 7M, 9M, "
            "12M, Type-A, or whatever your file actually contains) detected "
            "straight from your data - nothing is hardcoded. Exclude: use this "
            "for clients who don't need the box at all."
        )
    )
    include_model_box = (model_choice == "Include")

    # ---- Choose how labels are added ----
    st.header("📁 Add Labels")
    entry_mode = st.radio(
        "How would you like to add labels?",
        options=["Upload file (bulk)", "Manual entry (1-2 labels)"],
        index=0,
        horizontal=True,
        help="Use bulk upload for many labels at once. Use manual entry when you just need one or two labels quickly."
    )

    if entry_mode == "Upload file (bulk)":
        _run_bulk_upload_flow(include_model_box)
    else:
        _run_manual_entry_flow(include_model_box)


def _run_bulk_upload_flow(include_model_box):
    st.caption("New to this? Download a blank template, fill it in, then upload it below.")
    st.download_button(
        label="📄 Download blank mastersheet template",
        data=create_blank_mastersheet_bytes(),
        file_name="mastersheet_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    uploaded_file = st.file_uploader(
        "Choose an Excel or CSV file",
        type=['xlsx', 'xls', 'csv'],
        help="Upload your Excel or CSV file containing part information"
    )

    if uploaded_file is not None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploaded_file.name)[1]) as tmp_file:
            tmp_file.write(uploaded_file.getvalue())
            temp_input_path = tmp_file.name

        st.success(f"✅ File uploaded: {uploaded_file.name}")

        try:
            if uploaded_file.name.lower().endswith('.csv'):
                preview_df = pd.read_csv(temp_input_path).head(5)
            else:
                preview_df = pd.read_excel(temp_input_path).head(5)

            st.subheader("📊 Data Preview (First 5 rows)")
            st.dataframe(preview_df, use_container_width=True)

        except Exception as e:
            st.error(f"Error previewing file: {e}")
            return

        # Column mapping section
        st.subheader("🔧 Column Detection")

        try:
            if uploaded_file.name.lower().endswith('.csv'):
                df_full = pd.read_csv(temp_input_path)
            else:
                df_full = pd.read_excel(temp_input_path)

            cols_upper = [col.upper() if isinstance(col, str) else col for col in df_full.columns]

            part_no_col = next((col for col in cols_upper if 'PART' in col and ('NO' in col or 'NUM' in col or '#' in col)),
                             next((col for col in cols_upper if col in ['PARTNO', 'PART']), cols_upper[0] if cols_upper else ''))

            desc_col = next((col for col in cols_upper if 'DESC' in col),
                           next((col for col in cols_upper if 'NAME' in col), cols_upper[1] if len(cols_upper) > 1 else ''))

            qty_bin_col = next((col for col in cols_upper if 'QTY/BIN' in col or 'QTY_BIN' in col or 'QTYBIN' in col),
                              next((col for col in cols_upper if 'QTY' in col and 'BIN' in col),
                                   next((col for col in cols_upper if 'QTY' in col), '')))

            loc_col = next((col for col in cols_upper if 'LOC' in col or 'POS' in col or 'LOCATION' in col), '')

            qty_veh_col_disp = next((col for col in cols_upper if any(term in col for term in ['QTY/VEH', 'QTY_VEH', 'QTY PER VEH', 'QTYVEH'])), '')

            model_col_disp = find_model_column(df_full.columns.tolist())

            col1, col2 = st.columns(2)

            with col1:
                st.info(f"**Part Number Column:** {part_no_col}")
                st.info(f"**Description Column:** {desc_col}")
                st.info(f"**Location Column:** {loc_col}")

            with col2:
                st.info(f"**Qty/Bin Column:** {qty_bin_col}")
                st.info(f"**Qty/Vehicle Column:** {qty_veh_col_disp if qty_veh_col_disp else 'Not detected'}")
                st.info(f"**Model Column:** {model_col_disp if model_col_disp else 'Not detected'}")

            # Show which models were actually detected in this file
            if include_model_box:
                df_check = df_full.copy()
                df_check.columns = [c.upper() if isinstance(c, str) else c for c in df_check.columns]
                qty_veh_col_check = next((col for col in df_check.columns if any(term in str(col) for term in ['QTY/VEH', 'QTY_VEH', 'QTY PER VEH', 'QTYVEH', 'QTYPERCAR', 'QTYCAR', 'QTY/CAR'])), None)
                model_col_check = find_model_column(df_check.columns.tolist())
                detected_models = get_unique_models(df_check, model_col_check, qty_veh_col_check)
                if detected_models:
                    st.success(f"📋 Models detected in this file: {', '.join(detected_models)}")
                else:
                    st.warning("📋 Include is selected, but no model data was found in this file — the box won't be printed.")

        except Exception as e:
            st.error(f"Error analyzing columns: {e}")
            return

        # Generate labels section
        st.subheader("🚀 Generate Labels")

        col1, col2, col3 = st.columns([1, 1, 2])

        with col1:
            if st.button("🏷️ Generate PDF Labels", type="primary", use_container_width=True):
                status_container = st.empty()

                with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_output:
                    temp_output_path = tmp_output.name

                def update_status(message):
                    status_container.info(f"📊 {message}")

                try:
                    update_status("Starting label generation...")

                    result_path = generate_sticker_labels(
                        temp_input_path,
                        temp_output_path,
                        status_callback=update_status,
                        include_model_box=include_model_box
                    )

                    if result_path:
                        with open(result_path, 'rb') as pdf_file:
                            pdf_data = pdf_file.read()

                        status_container.success("✅ Labels generated successfully!")

                        st.download_button(
                            label="📥 Download PDF Labels",
                            data=pdf_data,
                            file_name=f"sticker_labels_{uploaded_file.name.split('.')[0]}.pdf",
                            mime="application/pdf",
                            use_container_width=True
                        )

                        file_size = len(pdf_data) / 1024
                        st.info(f"📄 PDF size: {file_size:.1f} KB | Pages: {len(df_full)}")

                    else:
                        status_container.error("❌ Failed to generate labels")

                except Exception as e:
                    status_container.error(f"❌ Error: {str(e)}")
                    st.exception(e)

                finally:
                    try:
                        if os.path.exists(temp_input_path):
                            os.unlink(temp_input_path)
                        if os.path.exists(temp_output_path):
                            os.unlink(temp_output_path)
                    except Exception:
                        pass

        with col2:
            if st.button("🔍 Preview Sample", use_container_width=True):
                st.info("Preview functionality - shows first label design")

        _render_info_sections()

    else:
        st.info("👆 Please upload an Excel or CSV file to get started")
        _render_bulk_instructions()
        _render_sample_data()


def _run_manual_entry_flow(include_model_box):
    if 'manual_labels' not in st.session_state:
        st.session_state.manual_labels = [{}]

    st.caption(
        "Fill in the blanks for each label below — no spreadsheet needed. "
        "Add more labels or remove ones you don't need."
    )

    to_remove = None
    for i, label in enumerate(st.session_state.manual_labels):
        cols = st.columns([1.2, 1.6, 0.9, 1.2, 0.8, 0.8, 1.3, 0.3])
        label['part_no'] = cols[0].text_input("Part No", value=label.get('part_no', ''), key=f"part_no_{i}")
        label['description'] = cols[1].text_input("Description", value=label.get('description', ''), key=f"desc_{i}")
        label['model'] = cols[2].text_input("Model", value=label.get('model', ''), key=f"model_{i}")
        label['location'] = cols[3].text_input("Location", value=label.get('location', ''), key=f"loc_{i}")
        label['qty_bin'] = cols[4].text_input("Qty/Bin", value=label.get('qty_bin', ''), key=f"qtybin_{i}")
        label['qty_veh'] = cols[5].text_input("Qty/Veh", value=label.get('qty_veh', ''), key=f"qtyveh_{i}")
        label['store_location'] = cols[6].text_input("Store Location", value=label.get('store_location', ''), key=f"storeloc_{i}")
        cols[7].markdown("<div style='height: 1.85em'></div>", unsafe_allow_html=True)
        if cols[7].button("✕", key=f"remove_{i}", help="Remove this label"):
            to_remove = i

    if to_remove is not None:
        if len(st.session_state.manual_labels) > 1:
            st.session_state.manual_labels.pop(to_remove)
        else:
            st.session_state.manual_labels[0] = {}
        st.rerun()

    if st.button("+ Add label"):
        st.session_state.manual_labels.append({})
        st.rerun()

    st.markdown("---")
    st.caption(
        "Need more than a couple of labels? Switch to **Upload file (bulk)** above, "
        "or download a blank template to fill in offline and upload later."
    )
    st.download_button(
        label="📄 Download blank mastersheet template",
        data=create_blank_mastersheet_bytes(),
        file_name="mastersheet_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    rows = []
    for label in st.session_state.manual_labels:
        if any(str(v).strip() for v in label.values()):
            rows.append({
                'Part No': label.get('part_no', ''),
                'Description': label.get('description', ''),
                'Model': label.get('model', ''),
                'Location': label.get('location', ''),
                'Qty/Bin': label.get('qty_bin', ''),
                'Qty/Veh': label.get('qty_veh', ''),
                'Store Location': label.get('store_location', ''),
            })

    st.subheader("🚀 Generate Labels")

    if not rows:
        st.info("Fill in at least one label's blanks (Part No, Description) to generate labels.")
        return

    if st.button("🏷️ Generate PDF Labels", type="primary"):
        manual_df = pd.DataFrame(rows)
        status_container = st.empty()

        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_output:
            temp_output_path = tmp_output.name

        def update_status(message):
            status_container.info(f"📊 {message}")

        try:
            update_status("Starting label generation...")

            result_path = generate_sticker_labels_core(
                manual_df,
                temp_output_path,
                status_callback=update_status,
                include_model_box=include_model_box
            )

            if result_path:
                with open(result_path, 'rb') as pdf_file:
                    pdf_data = pdf_file.read()

                status_container.success("✅ Labels generated successfully!")

                st.download_button(
                    label="📥 Download PDF Labels",
                    data=pdf_data,
                    file_name="sticker_labels_manual.pdf",
                    mime="application/pdf",
                    use_container_width=True
                )

                file_size = len(pdf_data) / 1024
                st.info(f"📄 PDF size: {file_size:.1f} KB | Labels: {len(manual_df)}")
            else:
                status_container.error("❌ Failed to generate labels")

        except Exception as e:
            status_container.error(f"❌ Error: {str(e)}")
            st.exception(e)

        finally:
            try:
                if os.path.exists(temp_output_path):
                    os.unlink(temp_output_path)
            except Exception:
                pass


def _render_info_sections():
    st.subheader("ℹ️ Label Information")

    info_col1, info_col2 = st.columns(2)

    with info_col1:
        st.markdown("""
        **Label Features:**
        - 📏 Standard sticker size (10cm x 15cm)
        - 🔢 QR code for each part (Part No, Qty, Delivery Location, Storage Location)
        - 📍 Location tracking
        - 🏷️ Model box — models detected directly from your data, Include/Exclude toggle
        - 📦 Quantity per bin/vehicle
        """)

    with info_col2:
        st.markdown("""
        **Supported Columns:**
        - Part Number/Part No
        - Description/Name
        - Location/Position
        - Qty/Bin, Quantity
        - Qty/Veh, Qty per Vehicle
        - Model/Product Type
        - Store Location
        """)


def _render_bulk_instructions():
    st.subheader("📋 Instructions")
    st.markdown("""
    1. **Choose Include or Exclude** for the Model Box in the sidebar
    2. **Download the blank template** (optional) and fill it in with your data
    3. **Upload your file** - Excel (.xlsx, .xls) or CSV format
    4. **Review data preview** - Check if your data looks correct
    5. **Verify column detection** - Ensure columns are properly identified
    6. **Generate labels** - Click the button to create your PDF
    7. **Download** - Get your professional sticker labels
    """)

    st.subheader("💡 Tips")
    st.markdown("""
    - Use clear column headers like "Part No", "Description", "Location"
    - Model labels are read straight from your file — "7M", "9M", "12M", "Type-A", or any
      other model names your product line uses; nothing is hardcoded
    - Include quantity information in "Qty/Bin" or "Qty/Veh" columns
    - Location strings will be automatically parsed into components
    - Clients that don't use models at all can simply pick **"Exclude"** in the sidebar
    - Only need 1 or 2 labels? Switch to **"Manual entry"** above instead of uploading a file
    """)


def _render_sample_data():
    st.subheader("📊 Sample Data Format")
    sample_data = pd.DataFrame({
        'Part No': ['ABC123', 'DEF456', 'GHI789'],
        'Description': ['Engine Filter', 'Brake Pad Set', 'Oil Filter'],
        'Location': ['A1_B2_C3', 'D4_E5_F6', 'G7_H8_I9'],
        'Qty/Bin': [5, 10, 8],
        'Qty/Veh': [2, 4, 1],
        'Model': ['9M', '12M', '7M']
    })
    st.dataframe(sample_data, use_container_width=True)


if __name__ == "__main__":
    main()
