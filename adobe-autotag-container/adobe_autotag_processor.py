"""
This script automates a comprehensive PDF processing pipeline that integrates Amazon S3, Adobe PDF Services, and
several Python libraries to handle various tasks such as downloading files, PDF manipulation, extraction of structured
data, and re-uploading the processed files back to S3.

Key Functionalities:
1. **Download from S3**:
    - Downloads a PDF file from an S3 bucket using Boto3.
    - The file is downloaded to the local environment for processing.

2. **PDF Processing**:
    - **Auto-Tagging for Accessibility**: Using Adobe PDF Services Autotag only (the tagged PDF).
    - **Local text/figure extract**: PyMuPDF builds TOC and image context (replaces Adobe Extract).
    - **Table of Contents (TOC)**: Generated from heading-sized text after Autotag.
    - **Custom Metadata**: XML metadata (such as title and dominant language) is injected into the PDF, improving its 
      accessibility and organization.

3. **Data Extraction**:
    - The script extracts images and other data from Excel files related to the PDF and uploads them to the S3 bucket.
    - It also handles unzipping files and organizing extracted content.

4. **Re-Upload to S3**:
    - After processing, the updated PDF (with metadata, TOC, and accessibility tagging) is uploaded back to S3 with a 
      new filename.
    - Extracted images and structured data are uploaded in organized directories in S3.

5. **Logging**:
    - Extensive logging is implemented throughout the script for tracking file names, successful operations, errors, 
      and results.
    - Logs key actions such as file downloads, metadata updates, file extraction, and final uploads.

6. **Additional Features**:
    - The script utilizes AWS Comprehend to detect the dominant language from extracted text and incorporates this 
      information into the PDF’s metadata.
    - Unzipped files are managed and organized into respective directories, and structured data (such as tables) is 
      processed for additional metadata and TOC generation.

Libraries and Services:
- **Boto3**: AWS SDK for Python to interact with S3.
- **PyMuPDF**: For editing and updating PDF files, including adding TOC and custom metadata.
- **OpenPyXL**: For extracting images from Excel files.
- **Adobe PDF Services**: Autotag only (tagged PDF + Autotag Excel report). Extract API is not used.
- **AWS Comprehend**: For detecting the dominant language in extracted text.

Environment Variables Required:
- `S3_BUCKET_NAME`: The name of the S3 bucket to download and upload the PDF file.
- `S3_FILE_KEY`: The key (path) of the PDF file in the S3 bucket.

This script is ideal for batch processing of PDFs that need to be made accessible, tagged, and analyzed for further use
in structured formats. It handles compliance with accessibility standards and ensures easy re-upload of enhanced PDFs 
and related content.
"""

import pandas as pd
import openpyxl
import ast
import os
import boto3
import logging
import json
import sys
from botocore.exceptions import ClientError
import sqlite3
import pymupdf
import json
import re
import zipfile
from pypdf import PdfReader, PdfWriter

from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
from adobe.pdfservices.operation.exception.exceptions import ServiceApiException, ServiceUsageException, SdkException
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
from adobe.pdfservices.operation.io.cloud_asset import CloudAsset
from adobe.pdfservices.operation.io.stream_asset import StreamAsset
from adobe.pdfservices.operation.pdf_services import PDFServices, ClientConfig
from adobe.pdfservices.operation.pdfjobs.jobs.autotag_pdf_job import AutotagPDFJob
from adobe.pdfservices.operation.pdfjobs.params.autotag_pdf.autotag_pdf_params import AutotagPDFParams
from adobe.pdfservices.operation.pdfjobs.result.autotag_pdf_result import AutotagPDFResult

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

s3 = boto3.client('s3')
CLOUDWATCH_NAMESPACE = "PDFAccessibility"


def emit_stats(filename, **counts):
    """Log STATS JSON and publish CloudWatch metrics (best-effort)."""
    payload = {"filename": filename, **counts}
    logging.info("STATS %s", json.dumps(payload))
    try:
        cw = boto3.client("cloudwatch")
        mapping = (
            ("adobe_calls", "AdobeApiCalls"),
            ("adobe_autotag", "AdobeAutotagJobs"),
            ("adobe_extract", "AdobeExtractJobs"),
            ("adobe_precheck", "AdobePrecheckJobs"),
            ("images_extracted", "ImagesExtracted"),
            ("toc_entries", "TocEntries"),
        )
        metric_data = []
        for key, metric_name in mapping:
            if key in counts and counts[key] is not None:
                metric_data.append({
                    "MetricName": metric_name,
                    "Value": float(counts[key]),
                    "Unit": "Count",
                    "Dimensions": [{"Name": "File", "Value": str(filename)[:250]}],
                })
        if metric_data:
            cw.put_metric_data(Namespace=CLOUDWATCH_NAMESPACE, MetricData=metric_data)
    except Exception as e:
        logging.warning("Filename : %s | CloudWatch stats emit failed: %s", filename, e)

def download_file_from_s3(bucket_name,file_base_name, file_key, local_path):
    """
    Download a file from an S3 bucket.
    
    Args:
        bucket_name (str): The S3 bucket name.
        file_base_name (str): The base name of the file.
        file_key (str): The key (path) of the file in the S3 bucket.
        local_path (str): The local path where the file will be saved.
    """
    logging.info(f"File key in the download_file_from_s3: {file_key}")
    s3.download_file(bucket_name, f"temp/{file_base_name}/{file_key}", local_path)
    logging.info(f"Downloaded {file_key} from {bucket_name} to {local_path}")

def save_to_s3(filename, bucket_name, folder_name,file_basename, file_key):
    """
    Uploads a file to an S3 bucket.
    
    Args:
        filename (str): The path of the file to upload.
        bucket_name (str): The S3 bucket name.
        folder_name (str): The folder where the file will be uploaded.
        file_basename (str): The base name of the file.
        file_key (str): The key (path) where the file will be uploaded.
    """

    with open(filename, "rb") as data:
        s3.upload_fileobj(data, bucket_name, f"temp/{file_basename}/{folder_name}/COMPLIANT_{file_key}")


def get_secret(basefilename):
    """
    Retrieves client credentials from AWS Secrets Manager.
    
    Args:
        basefilename (str): The base filename for logging purposes.
    
    Returns:
        tuple: (client_id, client_secret)
        
    Raises:
        ClientError: If there's an error retrieving the secret.
        KeyError: If the secret structure is invalid.
    """
    secret_name = "/myapp/client_credentials"
    region_name = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', os.environ.get('CDK_DEFAULT_REGION')))


    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )

    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        logging.error(f'Filename : {basefilename} | Failed to retrieve secret: {e}')
        raise  # Re-raise to stop the container

    secret = get_secret_value_response['SecretString']
    secret_dict = json.loads(secret)
    
    client_id = secret_dict['client_credentials']['PDF_SERVICES_CLIENT_ID']
    client_secret = secret_dict['client_credentials']['PDF_SERVICES_CLIENT_SECRET']
    
    return client_id, client_secret

def add_viewer_preferences(pdf_path, filename):
    reader = PdfReader(pdf_path)
    writer = PdfWriter()

    # Add all pages to the writer
    for page in reader.pages:
        writer.add_page(page)

    writer.create_viewer_preferences()
    writer.viewer_preferences.display_doctitle = True

    # Write the updated PDF to a file
    with open(filename, "wb") as f:
        writer.write(f)
    logger.info(f'Filename : {filename} | Viewer preferences added to the PDF')

def autotag_pdf_with_options(filename, client_id, client_secret):
    """
    Auto-tags a PDF for accessibility using Adobe PDF Services.
    
    Args:
        filename (str): The path to the PDF file.
        client_id (str): Adobe API client ID.
        client_secret (str): Adobe API client secret.
        
    Raises:
        ServiceApiException: If Adobe API returns an error.
        ServiceUsageException: If there's a usage-related error.
        SdkException: If there's an SDK-related error.
    """
    try:
        with open(filename, 'rb') as file:
            input_stream = file.read()
        

        # Initial setup, create credentials instance
        credentials = ServicePrincipalCredentials(
            client_id=client_id,
            client_secret=client_secret
        )
        client_config = ClientConfig(
            connect_timeout=8000,
            read_timeout=40000
        )

        # Creates a PDF Services instance
        pdf_services = PDFServices(credentials=credentials, client_config=client_config)

        # Creates an asset(s) from source file(s) and upload
        input_asset = pdf_services.upload(input_stream=input_stream,
                                        mime_type=PDFServicesMediaType.PDF)

        # Create parameters for the job
        autotag_pdf_params = AutotagPDFParams(
            generate_report=True,
            shift_headings=True
        )

        # Creates a new job instance
        autotag_pdf_job = AutotagPDFJob(input_asset=input_asset,
                                        autotag_pdf_params=autotag_pdf_params)

        # Submit the job and gets the job result
        location = pdf_services.submit(autotag_pdf_job)
        pdf_services_response = pdf_services.get_job_result(location, AutotagPDFResult)

        # Get content from the resulting asset(s)
        result_asset: CloudAsset = pdf_services_response.get_result().get_tagged_pdf()
        result_asset_report: CloudAsset = pdf_services_response.get_result().get_report()
        stream_asset: StreamAsset = pdf_services.get_content(result_asset)
        stream_asset_report: StreamAsset = pdf_services.get_content(result_asset_report)

        # Creates an output stream and copy stream asset's content to it
        os.makedirs("output/AutotagPDF", exist_ok=True)
        output_file_path = filename
        output_file_path_report = f"output/AutotagPDF/{filename}.xlsx"

        with open(output_file_path, "wb") as file:
            file.write(stream_asset.get_input_stream())
        with open(output_file_path_report, "wb") as file:
            file.write(stream_asset_report.get_input_stream())
        
        logging.info(f'Filename : {filename} | Adobe Autotag completed successfully')

    except (ServiceApiException, ServiceUsageException, SdkException) as e:
        logging.error(f'Filename : {filename} | Adobe Autotag API failed: {e}')
        raise  # Re-raise to stop the container

def build_local_structured_data(pdf_path, filename):
    """
    Replace Adobe Extract: build heading/text/figure elements with PyMuPDF
    after Autotag. Writes structuredData.json and figures/ for alt-text mapping.
    """
    extract_root = f"output/zipfile/{filename}"
    figures_dir = os.path.join(extract_root, "figures")
    os.makedirs(figures_dir, exist_ok=True)

    doc = pymupdf.open(pdf_path)
    elements = []
    figure_count = 0

    for page_index, page in enumerate(doc):
        page_height = page.rect.height
        blocks = page.get_text("dict").get("blocks", [])
        for block in blocks:
            bbox = block.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            x0, y0, x1, y1 = bbox
            # Adobe Extract Bounds are [left, bottom, right, top] (PDF origin).
            bounds = [x0, page_height - y1, x1, page_height - y0]
            if block.get("type") == 0:
                spans = [span for line in block.get("lines", []) for span in line.get("spans", [])]
                text = "".join(s.get("text", "") for s in spans).strip()
                if not text:
                    continue
                max_size = max((s.get("size", 0) for s in spans), default=0)
                path = "/Document/P"
                if max_size >= 18:
                    path = "/Document/H1"
                elif max_size >= 16:
                    path = "/Document/H2"
                elif max_size >= 14:
                    path = "/Document/H3"
                elif max_size >= 13:
                    path = "/Document/H4"
                elements.append({
                    "Path": path,
                    "Text": text,
                    "Page": page_index,
                    "Bounds": bounds,
                    "attributes": {"BBox": bounds},
                })
            elif block.get("type") == 1:
                try:
                    pix = page.get_pixmap(clip=pymupdf.Rect(bbox), dpi=144)
                    fig_name = f"figure{page_index}_{figure_count}.png"
                    fig_rel = f"figures/{fig_name}"
                    pix.save(os.path.join(extract_root, fig_rel))
                    figure_count += 1
                    elements.append({
                        "Path": "/Document/Figure",
                        "Page": page_index,
                        "Bounds": bounds,
                        "attributes": {"BBox": bounds},
                        "filePaths": [fig_rel],
                    })
                except Exception as e:
                    logging.warning("Filename : %s | Could not clip figure on page %s: %s", filename, page_index, e)

    data = {"elements": elements}
    os.makedirs(extract_root, exist_ok=True)
    with open(os.path.join(extract_root, "structuredData.json"), "w", encoding="utf-8") as file:
        json.dump(data, file)
    doc.close()
    logging.info(
        "Filename : %s | Local extract (PyMuPDF) wrote %s elements, %s figures",
        filename, len(elements), figure_count,
    )
    return data

def unzip_file(filename,zip_path, extract_to):
    """
    Unzips a zip file to a specified directory.
    
    Args:
        zip_path (str): The path of the zip file.
        extract_to (str): The directory where the contents will be extracted.
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"The file {zip_path} does not exist.")
    os.makedirs(extract_to, exist_ok=True)

    # Open the ZIP file
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        # Extract all contents
        zip_ref.extractall(extract_to)
        logging.info(f'Filename : {filename} |Files extracted to {extract_to}')

def add_toc_to_pdf(filename,pdf_document,data):
    """
    Adds a Table of Contents (TOC) to a PDF document based on the provided entries.
    """
    bookmarks = []
    for element in data.get("elements", []):
        path = element.get("Path", "")
        if re.search(r'H[1-4]', path) and "Text" in element:
            bookmarks.append((element["Text"], element["Page"] + 1))
        else:
            # Optional: Log elements without 'Text' or not matching headings
            if "Text" not in element:
                logging.debug(f"Element with ObjectID {element.get('ObjectID')} has no 'Text' key.")
            if not re.search(r'H[1-4]', path):
                logging.debug(f"Element with ObjectID {element.get('ObjectID')} does not match heading pattern.")

    # Create a list of toc entries in the format required by PyMuPDF
    toc_list = []
    for title, page_number in bookmarks:
        # Page numbers are 0-indexed in PyMuPDF
        toc_list.append([1, title, page_number])
    # Add TOC entries
    pdf_document.set_toc(toc_list)
    logging.info(f'Filename : {filename} |TOC entries added to the PDF')

# Currently done by Adobe API(May be required in the future)
def set_language_comprehend(filename,data,pdf_document):
    concatenated_text = ""
    for element in data['elements']:
        if 'Text' in element:
            concatenated_text += element['Text'] + " "

    # Remove trailing whitespace
    concatenated_text = concatenated_text.strip()
    
    comprehend = boto3.client('comprehend')
    response = comprehend.detect_dominant_language(Text=concatenated_text)
    languages = response['Languages']
    # Assuming the dominant language is the one with the highest score
    dominant_language = max(languages, key=lambda lang: lang['Score'])

    # Set the language in the PDF metadata
    pdf_document.set_language(dominant_language['LanguageCode'])
    logging.info(f'Filename : {filename} | Language set to {dominant_language["LanguageCode"]}')

def extract_images_from_extract_api(filename):
    # unzip_file(file_path, output_dir)
    
    with open(f"output/zipfile/{filename}/structuredData.json", "r") as file:
        data = json.load(file)
    by_page = {}

    for ele in data["elements"]:
        if "Page" in ele:
            if ele["Page"] not in by_page:
                by_page[ele["Page"]] = []
            by_page[ele["Page"]].append(ele)

    
    return by_page

def natural_sort_key(filename):
        # Extract numbers from the file name using regex and convert to int for sorting
        return [int(num) if num.isdigit() else num for num in re.split(r'(\d+)', filename)]

def is_bbox_match(api_bbox, excel_bbox,tol=7):
    """
    Returns True if the differences between the API bbox and the converted Excel bbox 
    are all within the specified tolerance.
    """
    api_left = round(api_bbox[0])
    api_bottom = round(api_bbox[1])
    api_right = round(api_bbox[2])
    api_top = round(api_bbox[3])
    
    api_width  = api_right - api_left
    api_height = api_top - api_bottom
    
    diff_width = abs(api_width - excel_bbox[2])
    diff_height = abs(api_height - excel_bbox[3])
    
    diff_x = abs(api_left-excel_bbox[0])
    diff_y = abs(api_top-excel_bbox[1])
    return (diff_width <= tol and diff_height <= tol and
            diff_x <= tol and diff_y <= tol)

def create_sqlite_db(by_page, filename, images_output_dir, object_ids, image_paths,
                     page_num_img, parsed_cordinates, bucket_name, s3_folder_autotag, file_key, object_ids_cords,
                     uploaded_s3_keys=None):
    # Build the SQLite database file path and create a new database.
    db_path = os.path.join(images_output_dir, "temp_images_data.db")
    if os.path.exists(db_path):
        os.remove(db_path)
        logging.info(f'Filename : {filename} | Removed existing SQLite DB')
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS image_data (
            objid TEXT,
            img_path TEXT,
            prev TEXT,
            current TEXT,
            next TEXT,
            context TEXT
        )
    """)
    
    # This set ensures that a candidate from the API is only assigned once.
    assigned_candidates = set()
    uploaded_s3_keys = uploaded_s3_keys or {}

    # Process each Excel row (each image from Excel)
    for objid, pg_num, excel_bbox in zip(object_ids, page_num_img, object_ids_cords):
        if pg_num not in by_page:
            logging.warning(f"Page {pg_num} not found in API data for file {filename}.")
            continue
        else:
            # --------------------------------------------------------------------
            # 1. Identify the current candidate using bounding box matching.
            # --------------------------------------------------------------------
            candidates = []

            for page, val in by_page.items():
                for ele in val:
                    # Enforce one-to-one mapping: skip already assigned elements.
                    if id(ele) in assigned_candidates:
                        logging.debug(f"Candidate {ele.get('ObjectID')} already assigned. Skipping.")
                        continue
                    if "filePaths" not in ele:
                        logging.debug(f"Candidate {ele.get('ObjectID')} has no filePaths. Skipping.")
                        continue

                    # Consider only file paths with image extensions.
                    paths = ele["filePaths"]
                    image_paths_list = [p for p in paths if p.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp'))]
                    if not image_paths_list:
                        logging.debug(f"Candidate {ele.get('ObjectID')} has no valid image file paths. Skipping.")
                        continue

                    candidate_image_path = image_paths_list[0]
                    if "tables" in candidate_image_path.lower():
                        logging.debug(f"Candidate {ele.get('ObjectID')} is from a tables directory. Skipping.")
                        continue

                    # Make sure the candidate has bounding box data.
                    if "attributes" not in ele or "BBox" not in ele["attributes"]:
                        logging.debug(f"Candidate {ele.get('ObjectID')} has no bounding box data. Skipping.")
                        continue

                    # Check if the candidate's bounding box matches the Excel bounding box.
                    if is_bbox_match(ele["Bounds"], excel_bbox[1], tol=7):
                        ele["objid"] = excel_bbox[0]
                        candidates.append(ele)

            # Prefer Auto-Tag uploaded objects over local PyMuPDF figure clips
            # (figure0_0.png is not the S3 object name).
            uploaded_candidates = [c for c in candidates if c.get("s3_key")]
            if uploaded_candidates:
                candidates = uploaded_candidates

            if len(candidates) == 1:
                current_candidate = candidates[0]
            elif len(candidates) > 1:
                current_candidate = candidates[0]
            else:
                current_candidate = None

            if not current_candidate:
                continue
            if current_candidate:
                assigned_candidates.add(id(current_candidate))
            # --------------------------------------------------------------------
            # 2. Build the whole page context string.
            # --------------------------------------------------------------------
            # The context string is built by iterating over all API elements for the page,
            # preserving their original order.
            context_parts = []
            for ele in by_page[pg_num]:
                # Append any text from the element.
                if "Text" in ele and ele["Text"]:
                    context_parts.append(ele["Text"])
                # If the element has image file paths (and valid image extensions), append an image marker.
                if "filePaths" in ele:
                    valid_paths = [p for p in ele["filePaths"]
                                   if p.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp'))
                                   and "tables" not in p.lower()]
                    if valid_paths:
                        image_name = os.path.basename(valid_paths[0])
                        if current_candidate is not None and id(ele) == id(current_candidate):
                            # This is the current image (the one matching the Excel row).
                            marker = f"<IMAGE INTERESTED>{image_name}</IMAGE INTERESTED>"
                        else:
                            # Other images on the page.
                            marker = f"<OTHER IMAGE>{image_name}</OTHER IMAGE>"
                        context_parts.append(marker)
            
            # Join all parts with a space.
            context = " ".join(context_parts)
        # Debug prints.
        print(f"{'<IMAGE INTERESTED>' in context}")
        print("context:", context)
        print(" ======================")
        stored_image_ref = current_candidate.get("s3_key")
        if not stored_image_ref:
            local_name = current_candidate["filePaths"][0].split("/")[-1]
            stored_image_ref = uploaded_s3_keys.get(
                local_name,
                f"{s3_folder_autotag}/images/{file_key}_{local_name}",
            )
        # Insert the actual S3 object key so alt-text does not invent a filename.
        cursor.execute("""
            INSERT INTO image_data (objid, img_path, context)
            VALUES (?, ?, ?)
        """, (
            current_candidate["objid"],
            stored_image_ref,
            context
        ))
        print("Added in the database: ", current_candidate["objid"], stored_image_ref)
        logging.info(
            "Filename : %s | SQLite image mapping objid=%s s3_key=%s",
            filename, current_candidate["objid"], stored_image_ref,
        )
    conn.commit()
    conn.close()
    logging.info(f'Filename : {filename} | SQLite DB created with image data')
    
    # Upload the SQLite DB to S3.
    s3.upload_file(os.path.join(images_output_dir, "temp_images_data.db"),
                     bucket_name,
                     f'{s3_folder_autotag}/{file_key}_temp_images_data.db')
    logging.info(f'Filename : {filename} | Uploaded SQLite DB to S3')


def extract_images_from_excel(filename, figure_path, autotag_report_path, images_output_dir, bucket_name, s3_folder_autotag, file_key):
    """
    Extract images from an Excel file and save them to a directory and upload them to S3.

    Args:
        filename (str): The filename (used for logging).
        figure_path (str): Path to figures (unused in this snippet).
        autotag_report_path (str): Path to the Excel file.
        images_output_dir (str): Directory to save the images.
        bucket_name (str): The S3 bucket.
        s3_folder_autotag (str): The S3 folder for autotag output.
        file_key (str): File key for S3 naming.
    """
    try:
        logging.info(f'Filename : {filename} | Extracting the images from excel file...')
        
        # Load the workbook and get the sheet (Images are in the "Figures" sheet)
        wb = openpyxl.load_workbook(autotag_report_path)
        wb.close()
        sheet = wb["Figures"]
        logging.info(f'Filename : {filename} | Sheet: {sheet.title}')
        logging.info(f'Filename : {filename} | Number of images: {len(sheet._images)}')
        
        # Load the workbook into a DataFrame for additional details.
        df = pd.read_excel(autotag_report_path, sheet_name="Figures")
        logging.info(f'Filename : {filename} | DF loaded: {str(df)}')
        # Get the object IDs
        object_ids = df["Unnamed: 4"].dropna().values[1:]
        # Get page numbers for images (adjusting for 0-indexing)
        page_num_img = df["Figures and Alt Text (excludes artifacts and decorative images)"].dropna().values[2:].astype(int)
        page_num_img = [int(i)-1 for i in page_num_img]
        
        os.makedirs(images_output_dir, exist_ok=True)
        os.makedirs(figure_path, exist_ok=True)

        by_page = extract_images_from_extract_api(filename)
        image_paths = []
        coordinates = df["Unnamed: 3"].dropna().values[1:]
        parsed_cordinates = [ast.literal_eval(item) for item in coordinates]
        object_ids_cords = [(objid, cords) for objid, cords in zip(object_ids, parsed_cordinates)]
        print("Object IDs and Coordinates:", object_ids_cords)
        logging.info(f'Filename : {filename} | Sheet: {sheet} , Sheet Images: {sheet._images}')

        # Prefer Autotag Excel embedded figures (no Adobe Extract zip).
        saved_excel = []
        for idx, img in enumerate(sheet._images):
            img_type = img.path.split('.')[-1]
            img_path = os.path.join(figure_path, f'image_{idx + 1}.{img_type}')
            with open(img_path, 'wb') as f:
                f.write(img._data())
            saved_excel.append(img_path)
            logging.info(f'Filename : {filename} | Image {idx + 1} saved as {img_path}')

        if saved_excel:
            image_paths = saved_excel
        else:
            image_paths = [
                os.path.join(figure_path, f)
                for f in sorted(os.listdir(figure_path), key=natural_sort_key)
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp'))
            ]

        # Align Autotag Excel bboxes with figure files so sqlite matching still works.
        uploaded_s3_keys = {}
        for idx, ((objid, cords), pg_num) in enumerate(zip(object_ids_cords, page_num_img)):
            if idx >= len(image_paths):
                break
            left, top, width, height = cords[0], cords[1], cords[2], cords[3]
            bounds = [left, top - height, left + width, top]
            fig_name = os.path.basename(image_paths[idx])
            s3_object_key = f"{s3_folder_autotag}/images/{file_key}_{fig_name}"
            uploaded_s3_keys[fig_name] = s3_object_key
            ele = {
                "Page": pg_num,
                "Bounds": bounds,
                "attributes": {"BBox": bounds},
                "filePaths": [f"figures/{fig_name}"],
                "ObjectID": objid,
                "s3_key": s3_object_key,
            }
            by_page.setdefault(pg_num, []).append(ele)
        
        for img_path in image_paths:
            object_name = f"{file_key}_{os.path.basename(img_path)}"
            s3_object_key = f"{s3_folder_autotag}/images/{object_name}"
            uploaded_s3_keys[os.path.basename(img_path)] = s3_object_key
            s3.upload_file(img_path, bucket_name, s3_object_key)
            logging.info(f'Filename : {filename} | Uploaded image to S3 key {s3_object_key}')
        logging.info(f'Filename : {filename} | Object IDs: {object_ids} : Image Paths: {image_paths}')

        create_sqlite_db(
            by_page, filename, images_output_dir, object_ids, image_paths,
            page_num_img, parsed_cordinates, bucket_name, s3_folder_autotag,
            file_key, object_ids_cords, uploaded_s3_keys=uploaded_s3_keys,
        )
    except Exception as e:
        logging.warning(f'Filename : {filename} | Image extract fallback: {e}')
        os.makedirs(images_output_dir, exist_ok=True)
        db_path = os.path.join(images_output_dir, "temp_images_data.db")
        if os.path.exists(db_path):
            os.remove(db_path)
            logging.info(f'Filename : {filename} | Removed existing SQLite DB')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS image_data (
                objid TEXT,
                img_path TEXT,
                prev TEXT,
                current TEXT,
                next TEXT,
                context TEXT
            )
        """)
        
        conn.commit()
        conn.close()
        logging.info(f'Filename : {filename} | SQLite DB created with image data')
        
        # Upload the SQLite DB to S3.
        s3.upload_file(os.path.join(images_output_dir, "temp_images_data.db"),
                        bucket_name,
                        f'{s3_folder_autotag}/{file_key}_temp_images_data.db')
        logging.info(f'Filename : {filename} | Uploaded SQLite DB to S3 With No Images')

def main():
    """
    Main function that coordinates the downloading, processing, and uploading of PDF files and associated content.
    """
    file_key = None
    file_base_name = None
    
    try:    
        bucket_name = os.getenv('S3_BUCKET_NAME')
        s3_file_key = os.getenv('S3_FILE_KEY')
        
        if not bucket_name or not s3_file_key:
            logging.error("Error: S3_BUCKET_NAME and S3_FILE_KEY environment variables are required.")
            sys.exit(1)
        
        file_key = s3_file_key.split('/')[2]
        file_base_name = s3_file_key.split('/')[1]
        logging.info(f'Filename : {file_key} | Bucket Name: {bucket_name}')

        # Define the local file path where the file will be saved
        local_file_path = os.path.basename(file_key)  # Save the file with its original name
        
        # Download the file from S3
        logging.info(f'Filename : {file_key} | Downloading file from S3...')
        download_file_from_s3(bucket_name, file_base_name, file_key, local_file_path)

        base_filename = os.path.basename(local_file_path)
        filename = "COMPLIANT_" + base_filename

        # Get Adobe API credentials
        logging.info(f'Filename : {file_key} | Retrieving Adobe API credentials...')
        client_id, client_secret = get_secret(base_filename)

        # Add viewer preferences
        logging.info(f'Filename : {file_key} | Adding viewer preferences...')
        add_viewer_preferences(local_file_path, filename)

        # Run Adobe Autotag API (only Adobe job in this container)
        logging.info(f'Filename : {file_key} | Running Adobe Autotag API...')
        autotag_pdf_with_options(filename, client_id, client_secret)

        logging.info(f'Filename : {file_key} | Building local extract (PyMuPDF, no Adobe Extract)...')
        data = build_local_structured_data(filename, filename)

        pdf_document = pymupdf.open(filename)

        # Add TOC entries
        logging.info(f'Filename : {file_key} | Adding TOC entries...')
        add_toc_to_pdf(filename, pdf_document, data)
        toc_count = len(pdf_document.get_toc() or [])

        pdf_document.saveIncr()
        pdf_document.close()
        
        logging.info(f'Filename : {file_key} | Uploading processed PDF to S3...')
        save_to_s3(filename, bucket_name, "output_autotag", file_base_name, file_key)

        logging.info(f"PDF saved with updated metadata and TOC. File location: COMPLIANT_{file_key}")

        figure_path = f"output/zipfile/{filename}/figures"
        autotag_report_path = f"output/AutotagPDF/{filename}.xlsx"
        images_output_dir = "output/zipfile/images"

        s3_folder_autotag = f"temp/{file_base_name}/output_autotag"
        
        logging.info(f'Filename : {file_key} | Extracting and uploading images...')
        extract_images_from_excel(filename, figure_path, autotag_report_path, images_output_dir, bucket_name, s3_folder_autotag, file_key)

        images_extracted = 0
        if os.path.isdir(figure_path):
            images_extracted = len([
                n for n in os.listdir(figure_path)
                if n.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp'))
            ])
        emit_stats(
            file_key,
            adobe_calls=1,
            adobe_autotag=1,
            adobe_extract=0,
            adobe_precheck=0,
            images_extracted=images_extracted,
            toc_entries=toc_count,
        )
        
        logging.info(f'Filename : {file_key} | Processing completed successfully')
        logger.info(f"File: {file_base_name}, Status: Succeeded in First ECS task")
        
    except (ServiceApiException, ServiceUsageException, SdkException) as e:
        logger.error(f"File: {file_base_name}, Status: Failed in First ECS task - Adobe API Error")
        logger.error(f"Filename : {file_key} | Adobe API Error: {e}")
        sys.exit(1)
    except ClientError as e:
        logger.error(f"File: {file_base_name}, Status: Failed in First ECS task - AWS Error")
        logger.error(f"Filename : {file_key} | AWS Error: {e}")
        sys.exit(1)
    except FileNotFoundError as e:
        logger.error(f"File: {file_base_name}, Status: Failed in First ECS task - File Not Found")
        logger.error(f"Filename : {file_key} | File Not Found Error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"File: {file_base_name}, Status: Failed in First ECS task")
        logger.error(f"Filename : {file_key} | Unexpected Error: {e}")
        sys.exit(1)
        
if __name__ == "__main__":
    main()