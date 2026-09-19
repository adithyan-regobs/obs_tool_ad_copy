"""
Turn a documentation file's S3 key into a readable title.

The title deliberately comes from the filename rather than from inside the
file. Reading the real title means downloading every object on every listing —
a few hundred megabytes once the library has a hundred documents in it — and
the only ways round that (ranged zip reads, a persistent cache) cost more than
the titles are worth here.

The trade: filenames are the contract. "Deploy an EKS Service via DevLift.docx"
reads well; "eks_v2_FINAL.docx" does not.
"""
import re

#: Browsers and file managers add these when a name collides. They are an
#: artefact of how the file arrived, never part of its title.
_DUPLICATE_SUFFIX = re.compile(r"\s*\(\d+\)$")


def title_from_filename(key: str) -> str:
    """
    A display title for an S3 key.

    Drops the folder and extension, turns separators into spaces, and strips a
    trailing "(1)" style duplicate marker. Capitalisation is left alone — the
    author's is better than anything inferred.
    """
    filename = key.rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = stem.replace("_", " ").replace("-", " ")
    stem = _DUPLICATE_SUFFIX.sub("", stem)
    return " ".join(stem.split()) or filename
