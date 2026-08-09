import yt_dlp

# Get all extractor classes
extractors = yt_dlp.list_extractor_classes()

for extractor in extractors:
    print(f"{extractor.IE_NAME}: {extractor.IE_DESC}")