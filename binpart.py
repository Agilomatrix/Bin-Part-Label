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

# Max number of "Store Loc N" columns we will ever look for in a file
MAX_STORE_LOC_CELLS = 9

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

# Define paragraph styles
bold_style = ParagraphStyle(name='Bold', fontName='Helvetica-Bold', fontSize=16, alignment=TA_CENTER, leading=14)
desc_style = ParagraphStyle(name='Description', fontName='Helvetica', fontSize=11, alignment=TA_CENTER, leading=12)
qty_style = ParagraphStyle(name='Quantity', fontName='Helvetica', fontSize=11, alignment=TA_CENTER, leading=12)


def find_bus_model_column(df_columns):
    """
    Enhanced function to find the bus model column with better detection
    """
    cols = [str(col).upper() for col in df_columns]

    # Priority order for bus model column detection
    patterns = [
        # Exact matches (highest priority)
        lambda col: col == 'BUS_MODEL',
        lambda col: col == 'BUSMODEL',
        lambda col: col == 'BUS MODEL',
        lambda col: col == 'MODEL',
        lambda col: col == 'BUS_TYPE',
        lambda col: col == 'BUSTYPE',
        lambda col: col == 'BUS TYPE',
        lambda col: col == 'VEHICLE_TYPE',
        lambda col: col == 'VEHICLETYPE',
        lambda col: col == 'VEHICLE TYPE',
        # Partial matches (lower priority)
        lambda col: 'BUS' in col and 'MODEL' in col,
        lambda col: 'BUS' in col and 'TYPE' in col,
        lambda col: 'VEHICLE' in col and 'MODEL' in col,
        lambda col: 'VEHICLE' in col and 'TYPE' in col,
        lambda col: 'MODEL' in col,
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
    Normalize a raw bus-model value into a clean label.
    Handles things like '9m', '9 M', '9' -> '9M'.
    Anything that isn't a bare number or number+M is kept as-is (uppercased),
    so non-numeric model names (e.g. 'LOW FLOOR', 'STD') still work.
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


def get_unique_bus_models(df, bus_model_col, qty_veh_col):
    """
    Scan the whole file and collect the distinct bus model labels that
    actually appear in it (no hardcoded 7M/9M/12M) - so this adapts to
    whatever fleet a given client uses.
    """
    models = set()

    if bus_model_col and bus_model_col in df.columns:
        for v in df[bus_model_col].dropna():
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


def detect_model_qty_map(row, qty_veh_col, bus_model_col, all_models):
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

    # Method 2: dedicated bus model column tells us which model this row is
    if bus_model_col and bus_model_col in row and pd.notna(row[bus_model_col]):
        token = normalize_model_token(row[bus_model_col])
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
    """
    Parse a location string into components for table display.
    Line Location now has 6 cells (Rack No 1st+2nd digit were merged into
    a single "Rack No" cell), so this returns 6 parts instead of 7.
    """
    location_parts = [''] * 6

    if not location_str or not isinstance(location_str, str):
        return location_parts

    location_str = location_str.strip()
    if location_str.lower() in ['nan', 'none', 'null']:
        return location_parts

    pattern = r'([^_\s]+)'
    matches = re.findall(pattern, location_str)

    for i, match in enumerate(matches[:6]):
        if match.lower() not in ['nan', 'none', 'null']:
            location_parts[i] = match

    return location_parts


def _clean_digit_value(v):
    """Small helper: turn '1.0' -> '1', strip blanks/NaN-like strings to ''."""
    if v is None:
        return ''
    v = str(v).strip()
    if not v or v.lower() in ['nan', 'none', 'null']:
        return ''
    if re.match(r'^\d+\.0$', v):
        v = str(int(float(v)))
    return v


def _norm_key(name):
    """
    Normalize a column header for matching: uppercase and strip out ALL
    spaces/underscores, so 'Store Location 1', 'STORE_LOCATION_1' and
    'StoreLocation1' are all treated as the same key. This is what lets
    column detection survive real-world header formatting differences.
    """
    return re.sub(r'[\s_]+', '', str(name).upper().strip())


def _get_val_by_names(row_data, possible_names, default=''):
    """
    Look up a value in a row by trying a list of possible header names,
    matched via _norm_key (so spacing/underscore differences don't matter).
    Returns the cleaned string value, or default if not found/blank.
    """
    norm_map = {}
    for k in row_data.index:
        norm_map[_norm_key(k)] = k
    for name in possible_names:
        nk = _norm_key(name)
        if nk in norm_map:
            val = row_data[norm_map[nk]]
            if pd.notna(val) and str(val).strip().lower() not in ['nan', 'none', 'null', '']:
                return _clean_digit_value(val)
    return default


def extract_location_data_from_excel(row_data):
    """
    Extract location data from Excel row for Line Location (6 cells):
    Model, Station No, Storage Type, Rack No, Level, Cell.

    Column matching is flexible (case/space/underscore-insensitive), so it
    works against headers like "MODEL", "STORAGE TYPE", "RACK NO" as-is.
    If a file instead has the older split "Rack No (1st digit)" / "Rack No
    (2nd digit)" columns, those are automatically merged into one Rack No
    value for backward compatibility.
    """
    model = _get_val_by_names(row_data, ['Bus Model', 'Model', 'Vehicle Model', 'Vehicle Type'])
    station_no = _get_val_by_names(row_data, ['Station No'])
    storage_type = _get_val_by_names(row_data, ['Storage Type', 'Rack Type', 'Rack'])

    rack_no = _get_val_by_names(row_data, ['Rack No', 'RackNo'])
    if not rack_no:
        d1 = _get_val_by_names(row_data, ['Rack No (1st digit)', 'Rack No 1st digit'])
        d2 = _get_val_by_names(row_data, ['Rack No (2nd digit)', 'Rack No 2nd digit'])
        rack_no = f"{d1}{d2}" if (d1 or d2) else ''

    level = _get_val_by_names(row_data, ['Level'])
    cell = _get_val_by_names(row_data, ['Cell'])

    return [model, station_no, storage_type, rack_no, level, cell]


def get_store_loc_column_count(df, max_cells=MAX_STORE_LOC_CELLS):
    """
    Scan the file's columns ONCE (whole file, not per-row) and return how
    many sequential store-location columns (N = 1..max_cells) are present.
    Matches BOTH naming styles - "Store Loc N" and "Store Location N" -
    regardless of spacing/underscores (e.g. "Store Location 1",
    "STORE_LOC_1", "StoreLocation1" all count).
    Every label generated from this file uses this same cell count, e.g.
    if the file only has "Store Location 1".."Store Location 6" then every
    label's Store Location box has 6 cells; up to 9 gives 9 cells.
    Returns 0 if no matching columns are found at all.
    """
    norm_cols = {_norm_key(c) for c in df.columns}

    count = 0
    for i in range(1, max_cells + 1):
        candidates = {f'STORELOC{i}', f'STORELOCATION{i}'}
        if candidates & norm_cols:
            count = i
        else:
            # Stop at the first gap so the numbering stays sequential
            # (e.g. "Store Location 1" + "Store Location 3" with no
            # "Store Location 2" is treated as just 1 cell).
            break
    return count


def extract_store_location_data_from_excel(row_data, num_cells):
    """
    Extract Store Location values from the generic 'Store Loc N' /
    'Store Location N' columns (N = num_cells, detected once per file via
    get_store_loc_column_count). Always returns a list of exactly
    num_cells values so every label in the batch has the same width;
    a cell is '' if this particular row has no value for it.
    """
    norm_map = {_norm_key(k): k for k in row_data.index}

    values = []
    for i in range(1, num_cells + 1):
        candidates = [f'STORELOC{i}', f'STORELOCATION{i}']
        val = ''
        for cand in candidates:
            if cand in norm_map:
                raw = row_data[norm_map[cand]]
                if pd.notna(raw) and str(raw).strip().lower() not in ['nan', 'none', 'null', '']:
                    val = _clean_digit_value(raw)
                break
        values.append(val)
    return values


def generate_sticker_labels(excel_file_path, output_pdf_path, status_callback=None, include_mtm_box=True):
    """
    Generate sticker labels with QR code from Excel data.

    include_mtm_box: True to print the bus-model box, False for clients
    that don't use it at all. The model labels themselves (e.g. 7M, 9M,
    12M, or whatever a given client's fleet uses) are always detected
    directly from the uploaded file - nothing is hardcoded.

    Line Location is 6 cells: Model, Station No, Storage Type, Rack No,
    Level, Cell - matched flexibly against your headers (e.g. "MODEL",
    "STORAGE TYPE", "RACK NO" all work as-is).

    Store Location is dynamic: it reads "Store Loc N" / "Store Location N"
    columns straight from the uploaded file (N auto-detected, up to
    MAX_STORE_LOC_CELLS), so a file with only 3 such columns prints 3
    cells and a file with up to 9 prints 9 cells.
    """
    def log(msg):
        if status_callback:
            status_callback(msg)
        else:
            st.write(msg)

    log(f"Processing file: {excel_file_path}")

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

    # Identify columns (case-insensitive)
    original_columns = df.columns.tolist()
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
    bus_model_col = find_bus_model_column(cols)

    log(f"Using columns: Part No: {part_no_col}, Description: {desc_col}, Location: {loc_col}, Qty/Bin: {qty_bin_col}")
    if qty_veh_col:
        log(f"Qty/Veh Column: {qty_veh_col}")
    if store_loc_col:
        log(f"Store Location Column: {store_loc_col}")
    if bus_model_col:
        log(f"Bus Model Column: {bus_model_col}")

    # ---- Resolve the bus-model box: which models exist in this file? ----
    all_models = []
    render_mtm_box = False
    if include_mtm_box:
        all_models = get_unique_bus_models(df, bus_model_col, qty_veh_col)
        if all_models:
            render_mtm_box = True
            log(f"Bus Model Box: Include -> detected models in file: {', '.join(all_models)}")
        else:
            log("Bus Model Box: Include was selected, but no bus-model data was found in "
                "the file, so no box will be printed.")
    else:
        log("Bus Model Box: Exclude -> no bus-model box will be printed")

    # ---- Resolve the Store Location box width once for the whole file ----
    num_store_cells = get_store_loc_column_count(df, max_cells=MAX_STORE_LOC_CELLS)
    if num_store_cells == 0:
        log("⚠️ No 'Store Loc N' / 'Store Location N' columns detected in file — "
            "Store Location box will print with a single empty cell.")
        num_store_cells = 1
    else:
        log(f"Store Location: detected {num_store_cells} cell(s) "
            f"(Store Location 1..{num_store_cells}) in this file")

    store_font_size = 9 if num_store_cells <= 6 else (8 if num_store_cells <= 8 else 7)

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

    # Layout for the Line Location table (6 cells): Model, Station No,
    # Storage Type, Rack No, Level, Cell.
    inner_table_width = content_width * 2 / 3
    line_col_proportions = [1.8, 2.4, 0.7, 1.4, 0.7, 0.9]
    line_total_proportion = sum(line_col_proportions)
    line_inner_col_widths = [w * inner_table_width / line_total_proportion for w in line_col_proportions]

    # Layout for the Store Location table: num_store_cells equal-width cells.
    store_inner_col_widths = [inner_table_width / num_store_cells] * num_store_cells

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

        # Store Location section (dynamic Store Loc 1..N cells)
        store_loc_label = Paragraph("Store Location", ParagraphStyle(
        name='StoreLoc', fontName='Helvetica-Bold', fontSize=11, alignment=TA_CENTER
        ))

        store_loc_values = extract_store_location_data_from_excel(row, num_store_cells)

        store_loc_inner_table = Table(
            [store_loc_values],
            colWidths=store_inner_col_widths,
            rowHeights=[location_row_height]
        )
        store_loc_inner_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 1.2, colors.Color(0, 0, 0, alpha=0.95)),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), store_font_size),
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

        # Line Location section (6 cells - Rack No 1st+2nd merged)
        line_loc_label = Paragraph("Line Location", ParagraphStyle(
            name='LineLoc', fontName='Helvetica-Bold', fontSize=11, alignment=TA_CENTER
        ))
        location_parts = extract_location_data_from_excel(row)
        line_loc_inner_table = Table(
            [location_parts],
            colWidths=line_inner_col_widths,
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

        # ---- Bottom section: MTM box (optional) + QR code ----
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

        if render_mtm_box:
            model_quantities = detect_model_qty_map(row, qty_veh_col, bus_model_col, all_models)

            n_models = len(all_models)
            mtm_row_height = 1.5*cm

            # Reserve roughly the same footprint as before for the box area,
            # but size each column to fit however many models this file has.
            reserved_area_width = content_width - qr_width - 1.6*cm  # minus spacers
            mtm_box_width = reserved_area_width / n_models
            mtm_box_width = max(0.65*cm, min(1.4*cm, mtm_box_width))

            header_row = list(all_models)
            value_row = [
                Paragraph(f"<b>{model_quantities[model]}</b>", ParagraphStyle(
                    name=f'BoldModel_{model}', fontName='Helvetica-Bold', fontSize=9, alignment=TA_CENTER
                )) if model_quantities[model] else ""
                for model in all_models
            ]

            mtm_table = Table(
                [header_row, value_row],
                colWidths=[mtm_box_width] * n_models,
                rowHeights=[mtm_row_height/2, mtm_row_height/2]
            )

            mtm_table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 1.2, colors.Color(0, 0, 0, alpha=0.95)),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 8 if n_models > 4 else 9),
                ('FONTSIZE', (0, 1), (-1, 1), 9),
            ]))

            mtm_table_width = mtm_box_width * n_models
            right_margin = 0.3*cm
            middle_spacer_width = max(0.3*cm, content_width - mtm_table_width - qr_width - right_margin)

            # MTM box stays on the left; QR code is pushed to the right edge
            bottom_row = Table(
                [[mtm_table, "", qr_table, ""]],
                colWidths=[mtm_table_width, middle_spacer_width, qr_width, right_margin],
                rowHeights=[qr_height]
            )
        else:
            # No MTM box: push the QR code to the right edge of the label
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


LOGO_CANDIDATE_PATHS = [
    "Agilomatrix logo.png",
    "agilomatrix_logo.png",
    "agilomatrix logo.png",
    "Agilomatrix_logo.png",
    "logo.png",
    "assets/Agilomatrix logo.png",
    "assets/agilomatrix_logo.png",
    "assets/logo.png",
    "images/Agilomatrix logo.png",
    "images/agilomatrix_logo.png",
    "static/agilomatrix_logo.png",
]


def _find_logo_path():
    """
    Locate the Agilomatrix logo file already sitting in the repo.
    First tries the exact candidate paths above (fast path). If none of
    those match - e.g. the repo file has different spacing/casing, like
    "Agilomatrix logo.png" - falls back to scanning the app's folder (and
    a few common subfolders) for any image file whose name contains
    "logo", case-insensitively, so small naming differences don't break it.
    """
    for path in LOGO_CANDIDATE_PATHS:
        if os.path.exists(path):
            return path

    search_dirs = [".", "assets", "images", "static"]
    image_exts = (".png", ".jpg", ".jpeg", ".webp")
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        try:
            for fname in os.listdir(d):
                lower = fname.lower()
                if "logo" in lower and lower.endswith(image_exts):
                    return os.path.join(d, fname)
        except OSError:
            continue

    return None


def build_blank_template_bytes():
    """
    Build a blank Bin Label Master Sheet template (headers only, a couple
    of example Store Location columns) as XLSX bytes, for the
    "Download blank mastersheet template" button.
    """
    columns = [
        'Part No', 'Description', 'Qty/Bin', 'Qty/Veh',
        'Model', 'Station No', 'Storage Type', 'Rack No', 'Level', 'Cell',
        'Store Location 1', 'Store Location 2', 'Store Location 3',
    ]
    template_df = pd.DataFrame(columns=columns)
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        template_df.to_excel(writer, index=False, sheet_name='Master Sheet')
    buf.seek(0)
    return buf.getvalue()


def main():
    """Main Streamlit application"""
    st.set_page_config(page_title="Bin Label Generator", page_icon="🏷️", layout="wide")

    # ---- Shared style tweaks (gradient divider + tagline styling) ----
    st.markdown("""
        <style>
        .agilo-tagline {
            text-align: center;
            font-size: 19px;
            letter-spacing: 0.06em;
            color: #475569;
            font-weight: 600;
            margin-top: -6px;
        }
        .agilo-gradient-bar {
            height: 4px;
            width: 340px;
            max-width: 60%;
            margin: 18px auto 26px auto;
            border-radius: 2px;
            background: linear-gradient(to right, #3B82F6 0%, #10B981 33%, #EC4899 66%, #F97316 100%);
        }
        .agilo-subtext {
            text-align: center;
            color: #64748B;
            font-size: 16px;
            max-width: 760px;
            margin: 0 auto;
        }
        .agilo-credit {
            text-align: center;
            font-style: italic;
            color: #94A3B8;
            font-size: 14px;
            margin-top: 6px;
        }
        </style>
    """, unsafe_allow_html=True)

    # ---- Sidebar: Settings ----
    st.sidebar.header("Settings")

    st.sidebar.subheader("Bus Model Box")
    mtm_choice = st.sidebar.radio(
        "Print the bus-model box on the labels?",
        options=["Include", "Exclude"],
        index=0,
        help=(
            "Include: the bus-model box is printed, with model labels (7M, 9M, 12M, "
            "or whatever your file actually contains) detected straight from the "
            "uploaded data - nothing is hardcoded. Exclude: use this for clients "
            "who don't need the box at all."
        )
    )
    include_mtm_box = (mtm_choice == "Include")

    st.sidebar.markdown("---")
    st.sidebar.subheader("Upload the Mastersheet")
    uploaded_file = st.sidebar.file_uploader(
        "Upload mastersheet (.XLSX)",
        type=['xlsx', 'xls', 'csv'],
        help="200MB per file • XLSX / XLS / CSV",
        label_visibility="collapsed",
    )

    st.sidebar.download_button(
        label="Download blank mastersheet template",
        data=build_blank_template_bytes(),
        file_name="Bin_Label_Master_Sheet_Template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    # ---- Main header: logo, title, tagline, gradient bar, blurb ----
    header_l, header_c, header_r = st.columns([1, 2, 1])
    with header_c:
        logo_path = _find_logo_path()
        if logo_path:
            logo_l, logo_mid, logo_r = st.columns([1, 1, 1])
            with logo_mid:
                st.image(logo_path, width=180)
        st.markdown("<h1 style='text-align:center; margin-bottom:0;'>🏷️ Bin Label Generator</h1>",
                    unsafe_allow_html=True)
        st.markdown("<div class='agilo-tagline'>STICKER LABEL GENERATOR</div>", unsafe_allow_html=True)
        st.markdown("<div class='agilo-gradient-bar'></div>", unsafe_allow_html=True)
        st.markdown(
            "<p class='agilo-subtext'>Upload your Bin Label Master Sheet in the sidebar to "
            "bulk-generate part stickers — each with a QR code, Line Location and Store "
            "Location boxes, and an optional bus-model box, all detected straight from "
            "your file. No spreadsheet to wrangle by hand.</p>",
            unsafe_allow_html=True
        )
        st.markdown("<p class='agilo-credit'>Designed and Developed by Agilomatrix</p>",
                    unsafe_allow_html=True)

    st.markdown("---")

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

            bus_model_col_disp = find_bus_model_column(df_full.columns.tolist())

            col1, col2 = st.columns(2)

            with col1:
                st.info(f"**Part Number Column:** {part_no_col}")
                st.info(f"**Description Column:** {desc_col}")
                st.info(f"**Location Column:** {loc_col}")

            with col2:
                st.info(f"**Qty/Bin Column:** {qty_bin_col}")
                st.info(f"**Qty/Vehicle Column:** {qty_veh_col_disp if qty_veh_col_disp else 'Not detected'}")
                st.info(f"**Bus Model Column:** {bus_model_col_disp if bus_model_col_disp else 'Not detected'}")

            # Show which bus models were actually detected in this file
            if include_mtm_box:
                df_check = df_full.copy()
                df_check.columns = [c.upper() if isinstance(c, str) else c for c in df_check.columns]
                qty_veh_col_check = next((col for col in df_check.columns if any(term in str(col) for term in ['QTY/VEH', 'QTY_VEH', 'QTY PER VEH', 'QTYVEH', 'QTYPERCAR', 'QTYCAR', 'QTY/CAR'])), None)
                bus_model_col_check = find_bus_model_column(df_check.columns.tolist())
                detected_models = get_unique_bus_models(df_check, bus_model_col_check, qty_veh_col_check)
                if detected_models:
                    st.success(f"🚌 Bus models detected in this file: {', '.join(detected_models)}")
                else:
                    st.warning("🚌 Include is selected, but no bus-model data was found in this file — the box won't be printed.")

            # Show how many Store Location cells were detected in this file
            df_store_check = df_full.copy()
            df_store_check.columns = [c.upper() if isinstance(c, str) else c for c in df_store_check.columns]
            store_cell_count = get_store_loc_column_count(df_store_check, max_cells=MAX_STORE_LOC_CELLS)
            if store_cell_count:
                st.success(f"📦 Store Location: detected {store_cell_count} cell(s) "
                           f"(Store Location 1..{store_cell_count}) in this file")
            else:
                st.warning("📦 No 'Store Loc N' / 'Store Location N' columns detected — the "
                           "Store Location box will print with a single empty cell. Add "
                           "columns named 'Store Location 1', 'Store Location 2', ... "
                           "to fill it in.")

        except Exception as e:
            st.error(f"Error analyzing columns: {e}")
            return

        # Generate labels section
        st.subheader("🚀 Generate Labels")

        col1, col2, col3 = st.columns([1, 1, 2])

        with col1:
            if st.button("🏷️ Generate PDF Labels", type="primary", use_container_width=True):
                progress_container = st.empty()
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
                        include_mtm_box=include_mtm_box
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

        # Additional information
        st.subheader("ℹ️ Label Information")

        info_col1, info_col2 = st.columns(2)

        with info_col1:
            st.markdown("""
            **Label Features:**
            - 📏 Standard sticker size (10cm x 15cm)
            - 🔢 QR code for each part (Part No, Qty, Delivery Location, Storage Location)
            - 📍 Line Location (6 cells: Model, Station No, Storage Type, Rack No, Level, Cell)
            - 📦 Store Location (dynamic: Store Location 1..N, N auto-detected per file, up to 9)
            - 🚌 Bus model box — models detected directly from your file, Include/Exclude toggle
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
            - Model/Bus Model/Vehicle Type
            - Storage Type, Rack No (single column — or the older split "Rack No (1st/2nd digit)" still works)
            - Store Location 1, Store Location 2, ... Store Location 9 — as many as your fleet needs
            """)

    else:
        st.info("👆 Please upload an Excel or CSV file to get started")

        st.subheader("📋 Instructions")
        st.markdown("""
        1. **Choose Include or Exclude** for the Bus Model Box in the sidebar
        2. **Upload your file** - Excel (.xlsx, .xls) or CSV format
        3. **Review data preview** - Check if your data looks correct
        4. **Verify column detection** - Ensure columns are properly identified
        5. **Generate labels** - Click the button to create your PDF
        6. **Download** - Get your professional sticker labels
        """)

        st.subheader("💡 Tips")
        st.markdown("""
        - Use clear column headers like "Part No", "Description", "Location"
        - Bus model labels are read straight from your file — "7M", "9M", "12M", or any
          other model names your fleet uses; nothing is hardcoded
        - **Line Location** has 6 cells: Model, Station No, Storage Type, Rack No, Level, Cell
          (column matching ignores case/spacing, so "MODEL", "STORAGE TYPE" etc. all work as-is;
          an older file with split "Rack No (1st digit)"/"Rack No (2nd digit)" columns still
          auto-merges into one Rack No cell)
        - **Store Location** is dynamic: add columns named "Store Location 1", "Store Location 2",
          up to "Store Location 9" (or "Store Loc 1", "Store Loc 2"... — both styles work) — the
          label only prints as many cells as your file actually has (e.g. only 3 such columns
          in the file -> a 3-cell box)
        - Include quantity information in "Qty/Bin" or "Qty/Veh" columns
        - Clients that don't use bus models at all can simply pick **"Exclude"** in the sidebar
        """)

        st.subheader("📊 Sample Data Format")
        sample_data = pd.DataFrame({
            'Part No': ['ABC123', 'DEF456', 'GHI789'],
            'Description': ['Engine Filter', 'Brake Pad Set', 'Oil Filter'],
            'Qty/Bin': [5, 10, 8],
            'Qty/Veh': [2, 4, 1],
            'Model': ['9M', '12M', '7M'],
            'Station No': ['ST-10', 'ST-10', 'ST-11'],
            'Storage Type': ['SH', 'SH', 'FL'],
            'Rack No': [12, 5, 21],
            'Level': ['A', 'B', 'A'],
            'Cell': [1, 3, 2],
            'Store Location 1': ['A', 'B', 'C'],
            'Store Location 2': ['01', '02', '03'],
            'Store Location 3': ['3', '', '5'],
        })
        st.dataframe(sample_data, use_container_width=True)


if __name__ == "__main__":
    main()
