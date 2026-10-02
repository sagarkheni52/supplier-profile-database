import base64
import json
import mimetypes
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
import hashlib
import re
import uuid
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup=None
try:
    import fitz  # PyMuPDF
    from PIL import Image
    from io import BytesIO
except ImportError:
    fitz = None
    Image = None
    BytesIO = None

ROOT=Path(__file__).resolve().parent
PORT=int(os.environ.get("AI_DB_PORT","8000"))
OPENAI_API_KEY=os.environ.get("OPENAI_API_KEY","").strip()
OPENAI_MODEL=os.environ.get("OPENAI_MODEL","gpt-5.1").strip()

SCHEMA={
  "type":"object",
  "additionalProperties":False,
  "properties":{
    "brochure_name":{"type":"string"},
    "website_url":{"type":"string"},
    "summary":{
      "type":"object","additionalProperties":False,
      "properties":{
        "primary_category":{"type":"string"},
        "supplier_found":{"type":"string"},
        "product_count":{"type":"integer"},
        "notes":{"type":"string"}
      },
      "required":["primary_category","supplier_found","product_count","notes"]
    },
    "supplier":{
      "type":"object","additionalProperties":False,
      "properties":{
        "name":{"type":"string"},
        "location":{"type":"string"},
        "contact":{"type":"string"},
        "website":{"type":"string"}
      },
      "required":["name","location","contact","website"]
    },
    "categories":{
      "type":"array","items":{
        "type":"object","additionalProperties":False,
        "properties":{"name":{"type":"string"},"reason":{"type":"string"}},
        "required":["name","reason"]
      }
    },
    "products":{
      "type":"array","items":{
        "type":"object","additionalProperties":False,
        "properties":{
          "product_name":{"type":"string"},
          "brand":{"type":"string"},
          "model":{"type":"string"},
          "specification":{"type":"string"},
          "category":{"type":"string"},
          "material":{"type":"string"},
          "capacity":{"type":"string"},
          "power":{"type":"string"},
          "speed":{"type":"string"},
          "weight":{"type":"string"},
          "dimensions":{"type":"string"},
          "technical_data":{"type":"string"},
          "review_notes":{"type":"string"},
          "confidence":{"type":"string"},
          "source_pages":{"type":"array","items":{"type":"integer"}},
          "source_url":{"type":"string"}
        },
        "required":["product_name","brand","model","specification","category","material","capacity","power","speed","weight","dimensions","technical_data","review_notes","confidence","source_pages","source_url"]
      }
    }
  },
  "required":["brochure_name","website_url","summary","supplier","categories","products"]
}

def _safe_slug(text, fallback="product"):
    text=re.sub(r"[^A-Za-z0-9._-]+","_",text or "").strip("_")
    return (text[:70] or fallback)

def attach_pdf_photos(pdf_bytes, result):
    """Extract embedded catalogue images from the product's source pages.
    If a page has no usable embedded image, save a rendered page image as a fallback.
    Returns product-level relative web paths.
    """
    if fitz is None or Image is None:
        return result

    try:
        doc=fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return result

    outdir=ROOT/"product_photos"
    outdir.mkdir(parents=True, exist_ok=True)

    brochure_hash=hashlib.sha1(pdf_bytes).hexdigest()[:12]
    global_seen={}
    products=result.get("products",[])

    for idx, product in enumerate(products):
        pages=product.get("source_pages") or []
        valid_pages=[]
        for n in pages:
            try:
                n=int(n)
                if 1 <= n <= len(doc): valid_pages.append(n)
            except Exception:
                pass
        valid_pages=list(dict.fromkeys(valid_pages))

        photos=[]
        for page_no in valid_pages:
            page=doc[page_no-1]
            image_infos=[]
            try:
                image_infos=page.get_images(full=True)
            except Exception:
                image_infos=[]

            usable_on_page=0
            for img in image_infos:
                if len(photos)>=6: break
                xref=img[0]
                try:
                    extracted=doc.extract_image(xref)
                    raw=extracted.get("image")
                    if not raw: continue
                    im=Image.open(BytesIO(raw))
                    w,h=im.size
                    # Ignore tiny logos/icons/decorative assets.
                    if w<180 or h<120 or w*h<50000: continue
                    ratio=max(w/h,h/w)
                    if ratio>8.0: continue
                    digest=hashlib.sha1(raw).hexdigest()
                    rel=global_seen.get(digest)
                    if not rel:
                        ext="jpg"
                        filename=f"{brochure_hash}_{idx+1:03d}_{uuid.uuid4().hex[:8]}.{ext}"
                        rel=f"product_photos/{filename}"
                        dest=ROOT/rel
                        if im.mode not in ("RGB","L"):
                            im=im.convert("RGB")
                        im.save(dest,"JPEG",quality=88,optimize=True)
                        global_seen[digest]=rel
                    if rel not in photos:
                        photos.append(rel)
                        usable_on_page+=1
                except Exception:
                    continue

            # If no embedded product-sized images were found on this page,
            # save a readable page snapshot as a fallback visual.
            if usable_on_page==0 and len(photos)<6:
                try:
                    pix=page.get_pixmap(matrix=fitz.Matrix(1.5,1.5), alpha=False)
                    raw=pix.tobytes("png")
                    digest=hashlib.sha1(raw).hexdigest()
                    rel=global_seen.get(digest)
                    if not rel:
                        filename=f"{brochure_hash}_{idx+1:03d}_page_{page_no}_{uuid.uuid4().hex[:8]}.jpg"
                        rel=f"product_photos/{filename}"
                        im=Image.open(BytesIO(raw)).convert("RGB")
                        im.thumbnail((1800,1800))
                        im.save(ROOT/rel,"JPEG",quality=84,optimize=True)
                        global_seen[digest]=rel
                    if rel not in photos: photos.append(rel)
                except Exception:
                    pass

        product["photos"]=photos[:6]

    doc.close()
    return result


def fetch_website_content(start_url, max_pages=12):
    """Fetch a bounded set of same-domain public web pages and return clean text."""
    if BeautifulSoup is None:
        raise RuntimeError("BeautifulSoup is not installed. Install it with: py -m pip install beautifulsoup4")

    parsed=urlparse(start_url)
    if parsed.scheme not in ("http","https") or not parsed.netloc:
        raise ValueError("Website URL must start with http:// or https://")

    root_domain=parsed.netloc.lower()
    queue=[start_url]
    seen=set()
    pages=[]
    headers={"User-Agent":"Mozilla/5.0 (compatible; SupplierProfileDatabase/1.0)"}

    while queue and len(pages)<max_pages:
        url=queue.pop(0).split("#",1)[0]
        if url in seen:
            continue
        seen.add(url)

        try:
            req=Request(url,headers=headers,method="GET")
            with urlopen(req,timeout=20) as r:
                final_url=r.geturl()
                ctype=(r.headers.get("Content-Type") or "").lower()
                body=r.read(3*1024*1024)
        except Exception:
            continue

        if "text/html" not in ctype:
            continue
        if urlparse(final_url).netloc.lower()!=root_domain:
            continue

        soup=BeautifulSoup(body.decode("utf-8","replace"),"html.parser")
        for tag in soup(["script","style","noscript","svg","template"]):
            tag.decompose()

        title=soup.title.get_text(" ",strip=True) if soup.title else ""
        text=soup.get_text("\n",strip=True)

        clean_lines=[]
        for line in text.splitlines():
            line=" ".join(line.split())
            if line and len(line)>=2:
                clean_lines.append(line)
        clean="\n".join(clean_lines)

        pages.append({"url":final_url,"title":title,"text":clean[:30000]})

        # Follow likely useful pages first.
        candidates=[]
        for a in soup.find_all("a",href=True):
            href=urljoin(final_url,a.get("href","")).split("#",1)[0]
            if urlparse(href).netloc.lower()!=root_domain:
                continue
            label=(a.get_text(" ",strip=True)+" "+href).lower()
            score=0
            for word in ("product","products","machine","machinery","catalog","catalogue","equipment","model","solution","product-category"):
                if word in label: score+=2
            if score>0:
                candidates.append((score,href))
        for _,href in sorted(candidates,reverse=True):
            if href not in seen and href not in queue:
                queue.append(href)

    return pages


def ai_review(payload):
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured on the server.")

    filename=payload.get("filename","brochure")
    mime=payload.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    website_url=(payload.get("website_url") or "").strip()
    data_url=payload.get("data_url","")
    if not data_url and not website_url:
        raise ValueError("Provide a brochure or website URL.")
    if data_url and not data_url.startswith("data:"):
        raise ValueError("Invalid brochure data.")
    try:
        pdf_bytes=base64.b64decode(data_url.split(",",1)[1]) if ("pdf" in mime or filename.lower().endswith(".pdf")) else b""
    except Exception:
        pdf_bytes=b""

    hint=payload.get("category_hint","")
    supplier_hint=payload.get("supplier_hint","")
    website_pages=[]
    website_text=""
    if website_url:
        website_pages=fetch_website_content(website_url)
        if not website_pages:
            raise RuntimeError("Could not read the supplier website. It may block automated access or require JavaScript/login.")
        website_text="\n\n".join(
            f"--- WEBSITE PAGE: {pg['url']} ---\nTITLE: {pg['title']}\n{pg['text']}"
            for pg in website_pages
        )

    if not data_url:
        file_content=None
        raw=""
    elif mime.startswith("image/"):
        file_content={
          "type":"input_image",
          "image_url":data_url,
          "detail":"high"
        }
    elif mime=="application/pdf" or filename.lower().endswith(".pdf"):
        file_content={
          "type":"input_file",
          "filename":filename,
          "file_data":data_url
        }
    else:
        raw=base64.b64decode(data_url.split(",",1)[1]).decode("utf-8","replace")
        file_content=None

    instructions="""You are a sourcing and product-catalogue analyst for a China supplier database.

Review the supplied brochure/catalogue carefully and convert it into clean PRODUCT-WISE records.

Rules:
1. One materially different product/model/specification/material = separate product record.
2. Different suppliers for the same product are NOT separate products.
3. Do not invent values. Use empty strings when a field is not stated.
4. Extract manufacturer/supplier information from the brochure when present.
5. Identify the most appropriate category and suggest a small category structure useful for a sourcing database.
6. For every product, extract the product name, brand, model, specification/version and technical data such as capacity, power, speed, dimensions, weight and material when available.
7. Pay attention to tables: many catalogues contain multiple models in one table. Create a separate record for each clearly distinct model/specification.
8. In review_notes explain important sourcing observations and any uncertainty, but do not make unsupported claims about quality, certification, compliance or performance.
9. confidence should be High, Medium or Low based on how clearly the brochure identifies the product.
10. For each product, source_pages must list the 1-based PDF page numbers where that exact product/model is shown. Use an empty list when the source is website-only.
11. For each product, source_url should identify the website page where the product was found, when applicable.
12. When reviewing a website, prioritize actual product/machine pages over About/News/Blog pages.
13. Return JSON matching the supplied schema exactly.
"""
    if hint:
        instructions += "\nUser category hint: "+hint
    if supplier_hint:
        instructions += "\nUser supplier hint: "+supplier_hint

    content=[{"type":"input_text","text":instructions}]
    if file_content:
        content.append(file_content)
    elif raw:
        content.append({"type":"input_text","text":"BROCHURE TEXT:\n"+raw})
    if website_text:
        content.append({"type":"input_text","text":"SUPPLIER WEBSITE CONTENT:\n"+website_text})

    body={
      "model":OPENAI_MODEL,
      "input":[{"role":"user","content":content}],
      "text":{
        "format":{
          "type":"json_schema",
          "name":"brochure_review",
          "strict":True,
          "schema":SCHEMA
        }
      }
    }

    req=Request(
      "https://api.openai.com/v1/responses",
      data=json.dumps(body).encode("utf-8"),
      headers={
        "Authorization":"Bearer "+OPENAI_API_KEY,
        "Content-Type":"application/json"
      },
      method="POST"
    )
    with urlopen(req,timeout=180) as r:
        result=json.loads(r.read().decode("utf-8"))

    out=result.get("output_text","").strip()
    if not out:
        # Fallback for SDK/API response variants.
        parts=[]
        for item in result.get("output",[]):
            for c in item.get("content",[]):
                if c.get("type")=="output_text":
                    parts.append(c.get("text",""))
        out="".join(parts).strip()
    if not out:
        raise RuntimeError("AI returned no structured review.")
    try:
        parsed=json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError("AI returned invalid JSON: "+str(e))

    # Automatically extract product-wise photos from PDF catalogue pages.
    if (mime=="application/pdf" or filename.lower().endswith(".pdf")) and pdf_bytes:
        parsed=attach_pdf_photos(pdf_bytes, parsed)
    parsed["website_url"]=website_url
    if not parsed.get("brochure_name"):
        parsed["brochure_name"]=filename
    return parsed

class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path!="/api/ai/review-brochure":
            self.send_error(404,"Not found")
            return
        try:
            n=int(self.headers.get("Content-Length","0"))
            if n>35*1024*1024:
                raise ValueError("Brochure is too large for this prototype (max 35 MB).")
            raw=self.rfile.read(n)
            payload=json.loads(raw.decode("utf-8"))
            result=ai_review(payload)
            self.send_json(200,result)
        except HTTPError as e:
            detail=e.read().decode("utf-8","replace")
            self.send_json(502,{"error":"OpenAI API error: "+detail[:1200]})
        except (URLError,TimeoutError) as e:
            self.send_json(502,{"error":"Could not reach OpenAI: "+str(e)})
        except Exception as e:
            self.send_json(400,{"error":str(e)})

    def send_json(self,status,obj):
        raw=json.dumps(obj,ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(raw)))
        self.send_header("Cache-Control","no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self,fmt,*args):
        print("[%s] %s"%(self.log_date_time_string(),fmt%args))

if __name__=="__main__":
    os.chdir(ROOT)
    print("Supplier Profile Database AI server")
    print("Open: http://localhost:%d/supplier_profile_database.html"%PORT)
    print("AI model:",OPENAI_MODEL)
    if not OPENAI_API_KEY:
        print("WARNING: OPENAI_API_KEY is not configured.")
    ThreadingHTTPServer(("127.0.0.1",PORT),Handler).serve_forever()
