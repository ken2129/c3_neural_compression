"""Fetch only preselected official COCO JPEGs; no zip or subset reselection."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import urllib.request
from PIL import Image


def fetch(selection_path, byte_limit=32*1024**2):
    selection_path = Path(selection_path)
    selection = json.loads(selection_path.read_text())
    root = selection_path.parent/'images'; root.mkdir(exist_ok=True)
    receipt = []; total = 0
    for row in selection['images']:
        name = row['file_name']
        if name != f"{row['id']:012d}.jpg":
            raise ValueError('Unexpected COCO filename')
        # Canonical S3 path keeps TLS verification enabled (the dotted alias has a mismatched certificate).
        url = f'https://s3.amazonaws.com/images.cocodataset.org/val2017/{name}'
        path = root/name
        if path.exists():
            data = path.read_bytes()
        else:
            # Bounded individual download; use inherited proxy and CA settings.
            with urllib.request.urlopen(url, timeout=30) as stream:
                data = stream.read(min(4*1024**2,byte_limit-total)+1)
            if len(data)>4*1024**2 or total+len(data)>byte_limit:
                raise RuntimeError('Image download storage ceiling exceeded')
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'JPEG' or image.size != (row['width'],row['height']):
                raise ValueError('COCO JPEG format/dimensions mismatch')
            image.verify()
        if not path.exists():
            with path.open('xb') as stream: stream.write(data)
        total += len(data)
        if total>byte_limit: raise RuntimeError('Image download storage ceiling exceeded')
        receipt.append({'image_id':row['id'],'file_name':name,'original_hw':[row['height'],row['width']],
                        'path':str(path.resolve()),'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'source':url})
        (selection_path.parent/'download_receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    manifest = {'schema_version':1,'image_ids':selection['image_ids'],
                'annotation_file':selection['annotation_file'],'annotation_sha256':selection['annotation_sha256'],
                'images':receipt}
    (selection_path.parent/'frozen_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps({'downloaded_images':len(receipt),'bytes':total,'AP_seen':False}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('selection');a=p.parse_args();fetch(a.selection)
