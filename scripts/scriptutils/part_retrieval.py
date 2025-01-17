import logging
import os
import urllib.request
import urllib.parse
import glob
from typing import List
from urllib.error import HTTPError

from Bio import Entrez, SeqIO
import sbol2
import sbol3
from sbol_utilities.helper_functions import flatten, unambiguous_dna_sequence
# TODO: switch string_to_display_id to sbol3 after resolution of https://github.com/SynBioDex/pySBOL3/issues/191
from sbol_utilities.excel_to_sbol import string_to_display_id, BASIC_PARTS_COLLECTION
from .directories import EXPORT_DIRECTORY, SBOL_EXPORT_NAME, extensions
from .package_specification import package_stem
from .conversions import convert2to3, convert_from_fasta, convert_from_genbank

GENBANK_CACHE_FILE = 'GenBank_imports.gb'
IGEM_SBOL2_TRANSIENT_CACHE_FILE = 'iGEM_SBOL2_imports.xml'  # Not stored since SBOL2 doesn't have stable serialization
IGEM_SBOL2_CACHE_FILE = 'iGEM_SBOL2_imports.nt'  # SBOL3 converted form of transient cache
IGEM_SBOL3_CACHE_FILE = 'iGEM_SBOL3_imports.nt'
IGEM_FASTA_CACHE_FILE = 'iGEM_raw_imports.fasta'

FASTA_iGEM_PATTERN = 'http://parts.igem.org/cgi/partsdb/composite_edit/putseq.cgi?part={}'
SBOL_iGEM_PATTERNS = ['https://synbiohub.org/public/igem/BBa_{}', 'https://synbiohub.org/public/igem/{}']
iGEM_SOURCE_PREFIX = 'http://parts.igem.org/'
NCBI_PREFIX = 'https://www.ncbi.nlm.nih.gov/nuccore/'


class ImportFile:
    """Record for a file in the package parts inventory, containing all information needed for collation"""

    def __init__(self, path: str, file_type: str = sbol3.SORTED_NTRIPLES, namespace: str = None):
        self.path = path
        if file_type not in extensions.keys():
            raise ValueError(f'Unknown file type: "{file_type}"')
        self.file_type = file_type
        self.namespace = namespace.removesuffix('/') if namespace else None
        self.doc = None

    def get_sbol3_doc(self) -> sbol3.Document:
        """Access a file's contents in SBOL3 format. If not loaded, they will be loaded.
        If not in SBOL3, they will be converted.

        :return: SBOL3 document for the file's contents
        """
        if self.doc:  # If the document already loaded, we can just return it
            pass
        # Otherwise, load the file, converting if necessary
        elif self.file_type == 'FASTA':  # FASTA should be read with NCBI and converted directly into SBOL3
            self.doc = convert_from_fasta(self.path, self.namespace)
        elif self.file_type == 'GenBank':  # GenBank --> SBOL2 --> SBOL3
            self.doc = convert_from_genbank(self.path, self.namespace)
        elif self.file_type == 'SBOL2':  # SBOL2 files should all have been turned to SBOL3 already
            logging.warning(f'Should not be importing directly from SBOL2: {self.path}')
            doc2 = sbol2.Document()
            doc2.read(self.path)
            self.doc = convert2to3(doc2)
        elif self.file_type == 'SBOL3':  # reading from SBOL3 is simple
            self.doc = sbol3.Document()
            self.doc.read(self.path)
        else:
            raise ValueError(f'Unknown file type: "{self.file_type}" for {self.path}')

        return self.doc


class PackageInventory:
    """List of all of the parts imported into a package in various files"""
    def __init__(self):
        self.files: set[ImportFile] = set()
        self.locations: dict[str, ImportFile] = {}
        self.aliases: dict[str, str] = {}

    def add(self, import_file, uri: str, *aliases: str) -> None:
        # make sure the file is tracked
        self.files.add(import_file)
        # add the entry for the URI
        self.locations[uri] = import_file
        # add URI and all aliases to alias mapping
        keys = set(aliases)
        keys.add(uri)
        for key in keys:
            if key in self.aliases:
                logging.warning(f'Inventory found duplicate of part {key}')
            self.aliases[key] = uri


# for canonicalizing IDs
# TODO: get more systematic about this; maybe in the sheet?
prefix_remappings = {
    'https://synbiohub.org/public/igem/BBa_':iGEM_SOURCE_PREFIX,
    'https://synbiohub.org/public/igem/': iGEM_SOURCE_PREFIX  # for any non-BBA parts
}

def remap_prefix(uri: str) -> str:
    # see if the URI hits any remapping
    for old,new in prefix_remappings.items():
        if uri.startswith(old):
            return new+uri.removeprefix(old)
    # if not, return as before
    return uri


def sbol_uri_to_accession(uri: str, prefix: str = NCBI_PREFIX, remaps: dict[str,str] = None) -> str:
    """Change an NCBI SBOL URI to an accession ID
    :param uri: to convert
    :param prefix: prefix to use with accession, defaulting to NCBI nuccore
    :return: equivalent accession ID
    """
    if remaps is None:
        remaps = {'_': '.'}
    accession = uri.removeprefix(prefix)
    for k, v in remaps.items():
        accession = accession.replace(k,v)
    return accession


def accession_to_sbol_uri(accession: str, prefix: str = NCBI_PREFIX) -> str:
    """Change an NCBI accession ID to an equivalent NCBI SBOL URI
    :param accession: to convert
    :param prefix: prefix to use with accession, defaulting to NCBI nuccore
    :return: equivalent URI
    """
    if not prefix.endswith('/'):
        prefix += '/'
    return f'{prefix}{string_to_display_id(accession)}'


def retrieve_genbank_accessions(ids: List[str], package: str) -> List[str]:
    """Retrieve a set of nucleotide accessions from GenBank
    :param ids: SBOL URIs to retrieve
    :param package: path where retrieved items should be stored
    :return: list of items retrieved
    """
    # GenBank pull:
    Entrez.email = 'engineering@igem.org'
    id_string = ','.join([sbol_uri_to_accession(i) for i in ids])  # Have to strip everything but the accession
    print(f'Attempting to retrieve {len(ids)} parts from NCBI: {id_string}')
    try:
        handle = Entrez.efetch(id=id_string, db='nucleotide', rettype='gb', retmode='text')
        retrieved = [r for r in SeqIO.parse(handle, 'gb')]
        # add retrieved records to cache
        cache_file = os.path.join(package, GENBANK_CACHE_FILE)
        print(f'Retrieved {len(retrieved)} records from NCBI; writing to {cache_file}')
        with open(cache_file, 'a') as out:
            for r in retrieved:
                out.write(r.format('gb'))
        return [accession_to_sbol_uri(r.id) for r in retrieved]  # add the accessions back in
    except HTTPError:
        print('NCBI retrieval failed')
        return []


def retrieve_igem_parts(ids: List[str], package: str) -> List[str]:
    """Retrieve a set of iGEM parts from SynBioHub when possible, direct from the Registry when not.
    :param ids: SBOL URIs to retrieve
    :param package: path where retrieved items should be stored
    :return: list of items retrieved
    """
    sbh_source = sbol2.partshop.PartShop('https://synbiohub.org')

    # load current cache, to write into
    doc = sbol2.Document()
    sbol_cache_file = os.path.join(package, IGEM_SBOL2_TRANSIENT_CACHE_FILE)
    if os.path.isfile(sbol_cache_file):  # read any current material to avoid overwriting
        doc.read(sbol_cache_file)

    # pull one ID at a time, because SynBioHub will give an error if we try to pull multiple and one is missing
    print(f'Attempting to retrieve {len(ids)} parts from iGEM')
    retrieved_fasta = ''
    retrieved_ids = []
    sbol_count = 0
    fasta_count = 0
    for i in ids:
        accession = sbol_uri_to_accession(i, prefix=iGEM_SOURCE_PREFIX, remaps={})
        # First try from SynBioHub:
        for template in SBOL_iGEM_PATTERNS:
            try:
                url = template.format(accession)
                print(f'Attempting to retrieve iGEM SBOL from SynBioHub: {url}')
                sbh_source.pull(url, doc)
                retrieved_ids.append(i)
                sbol_count += 1
                print(f'  Successfully retrieved from SynBioHub')
            except sbol2.SBOLError as err:
                if err.error_code() == sbol2.SBOLErrorCode.SBOL_ERROR_NOT_FOUND:
                    continue
                else:
                    raise err  # if it wasn't a "not found" error, fail upward
        # if that didn't work, try to get a FASTA from the iGEM parts repository:
        if i not in retrieved_ids:
            try:
                url = FASTA_iGEM_PATTERN.format(accession)
                print(f'  SynBioHub retrieval failed; attempting to retrieve FASTA from iGEM Registry: {url}')
                with urllib.request.urlopen(url, timeout=5) as f:
                    captured = f.read().decode('utf-8').strip()

                if unambiguous_dna_sequence(captured):
                    retrieved_fasta += f'> {accession}\n{captured}\n'
                    retrieved_ids.append(i)
                    fasta_count += 1
                    print(f'  Successfully retrieved from iGEM Registry')
                else:
                    print(f'  Retrieved text is not a DNA sequence: {captured}')
            except IOError:
                print('  Could not retrieve from iGEM Registry')

    # write retrieved materials
    if sbol_count > 0:
        print(f'Retrieved {sbol_count} iGEM SBOL2 records from SynBioHub, writing to {sbol_cache_file}')
        doc.write(sbol_cache_file)
    if fasta_count > 0:
        fasta_cache_file = os.path.join(package, IGEM_FASTA_CACHE_FILE)
        print(f'Retrieved {fasta_count} FASTA records from iGEM Registry, writing to {fasta_cache_file}')
        with open(fasta_cache_file, 'a') as out:
            out.write(retrieved_fasta)

    return retrieved_ids


def retrieve_synbiohub_parts(ids: List[str], package: str) -> List[str]:
    """Retrieve a set of SBOL parts from SynBioHub
    :param ids: SBOL URIs to retrieve
    :param package: path where retrieved items should be stored
    :return: list of items retrieved
    """
    sbh_sources = {}

    # load current cache, to write into
    doc = sbol2.Document()
    sbol_cache_file = os.path.join(package, IGEM_SBOL2_TRANSIENT_CACHE_FILE)
    if os.path.isfile(sbol_cache_file):  # read any current material to avoid overwriting
        doc.read(sbol_cache_file)

    # pull one ID at a time, because SynBioHub will give an error if we try to pull multiple and one is missing
    print(f'Attempting to retrieve {len(ids)} parts from iGEM')
    retrieved_ids = []
    for url in ids:
        # figure out the server to access from the URL
        p = urllib.parse.urlparse(url)
        server = urllib.parse.urlunparse([p.scheme, p.netloc, '', '', '', ''])
        if server not in sbh_sources:
            sbh_sources[server] = sbol2.partshop.PartShop(server)
        sbh_source = sbh_sources[server]
        # now retrieve from the server
        try:
            print(f'Attempting to retrieve SBOL from SynBioHub at {server}: {url}')
            sbh_source.pull(url, doc)
            retrieved_ids.append(url)
            print(f'  Successfully retrieved from SynBioHub')
        except sbol2.SBOLError as err:
            if err.error_code() == sbol2.SBOLErrorCode.SBOL_ERROR_NOT_FOUND:
                print(f'  SynBioHub retrieval failed')
            else:
                raise err  # if it wasn't a "not found" error, fail upward

    # write retrieved materials
    if len(retrieved_ids) > 0:
        print(f'Retrieved {len(retrieved_ids)} iGEM SBOL2 records from SynBioHub, writing to {sbol_cache_file}')
        doc.write(sbol_cache_file)

    return retrieved_ids


source_list = {
    NCBI_PREFIX: retrieve_genbank_accessions,
    'https://synbiohub.org/public/igem/': retrieve_igem_parts,
    'http://parts.igem.org/': retrieve_igem_parts,
    'https://synbiohub': retrieve_synbiohub_parts  # TODO: make this more general, to support other SBH sources
}


def retrieve_parts(ids: List[str], package: str) -> List[str]:
    """Attempt to download parts from various servers

    :param ids: list of URIs
    :param package: path of package to retrieve from
    :return: list of URIs successfully retrieved
    """
    "Attempt to collect all of the parts on the list"
    collected = []
    for prefix, retriever in source_list.items():
        matches = [i for i in ids if i.startswith(prefix)]
        ids = [i for i in ids if i not in matches]  # remove the ones we're going to try to avoid double-searching
        if len(matches) > 0:
            successes = retriever(matches, package)
            collected += successes
    return collected


def package_parts_inventory(package: str) -> PackageInventory:
    """Search all of the SBOL, GenBank, and FASTA files of a package to find what parts have been downloaded

    :param package: path of package to search
    :return: dictionary mapping URIs and alias URIs to available URIs
    """
    inventory = PackageInventory()

    # import FASTAs and GenBank
    for file in sorted(flatten(glob.glob(os.path.join(package, f'*{ext}')) for ext in extensions['FASTA'])):
        is_igem_cache = os.path.basename(file) == IGEM_FASTA_CACHE_FILE
        prefix = iGEM_SOURCE_PREFIX if is_igem_cache else package_stem(package)
        with open(file) as f:
            import_file = ImportFile(file, file_type='FASTA', namespace=prefix)
            for record in SeqIO.parse(f, "fasta"):
                inventory.add(import_file, accession_to_sbol_uri(record.id, prefix))

    for file in sorted(flatten(glob.glob(os.path.join(package, f'*{ext}')) for ext in extensions['GenBank'])):
        is_ncbi_cache = os.path.basename(file) == GENBANK_CACHE_FILE
        prefix = NCBI_PREFIX if is_ncbi_cache else package_stem(package)
        with open(file) as f:
            import_file = ImportFile(file, file_type='GenBank', namespace=prefix)
            for record in SeqIO.parse(f, "gb"):
                inventory.add(import_file, accession_to_sbol_uri(record.name, prefix),
                              accession_to_sbol_uri(record.id, prefix))

    # import SBOL2
    # for file in sorted(flatten(glob.glob(os.path.join(package, f'*{ext}')) for ext in extensions['SBOL2'])):
    #     doc = sbol2.Document()
    #     doc.read(file)
    #     import_file = ImportFile(file, file_type='SBOL2')
    #     cds = [obj for obj in doc if isinstance(obj,sbol2.ComponentDefinition)]
    #     for cd in cds:
    #         inventory.add(import_file, remap_prefix(cd.persistentIdentity), remap_prefix(cd.identity))

    # import SBOL3
    for rdf_type, patterns in extensions['SBOL3'].items():
        for file in sorted(flatten(glob.glob(os.path.join(package, f'*{ext}')) for ext in patterns)):
            doc = sbol3.Document()
            doc.read(file)
            import_file = ImportFile(file, file_type='SBOL3')
            ids = [obj.identity for obj in doc.objects if isinstance(obj, sbol3.Component)]
            for i in ids:
                inventory.add(import_file, i, remap_prefix(i))

    return inventory


def import_parts(package: str) -> list[str]:
    """Compare package specification and inventory and attempt to import all missing parts

    :param package: path of package to search
    :return: list of parts URIs imported
    """
    # First collect the package specification
    package_spec = sbol3.Document()
    package_spec.read(os.path.join(package, EXPORT_DIRECTORY, SBOL_EXPORT_NAME))
    package_parts = [p.lookup() for p in package_spec.find(BASIC_PARTS_COLLECTION).members]

    print(f'Package specification contains {len(package_parts)} parts')

    # Then collect the parts in the package directory
    inventory = package_parts_inventory(package)
    print(f'Found {len(inventory.locations)} parts cached in package design files')

    # Compare the parts lists to each other to figure out which elements are missing
    package_part_ids = {p.identity for p in package_parts}
    package_sequence_ids = {p.identity for p in package_parts if p.sequences}
    package_no_sequence_ids = {p.identity for p in package_parts if not p.sequences}
    inventory_part_ids_and_aliases = set(inventory.aliases.keys())
    both = package_part_ids & inventory_part_ids_and_aliases
    # note: package_only list isn't actually needed
    inventory_only = set(inventory.locations.keys()) - {inventory.aliases[i] for i in both}
    missing_sequences = package_no_sequence_ids - inventory_part_ids_and_aliases
    print(f' {len(package_sequence_ids)} have sequences in Excel, {len(both)} found in directory, '
          f'{len(missing_sequences)} not found')
    print(f' {len(inventory_only)} parts in directory are not used in package')
    if inventory_only:
        print(f' Found {len(inventory_only)} unused parts:' + " ".join(p for p in inventory_only))

    # attempt to retrieve missing parts
    if len(missing_sequences) == 0:
        print('No missing sequences')
        return []
    else:
        print('Attempting to download missing parts')
        download_list = list(missing_sequences)
        download_list.sort()
        retrieved = retrieve_parts(download_list, package)
        print(f'Retrieved {len(retrieved)} out of {len(missing_sequences)} missing sequences')
        print(retrieved)
        still_missing = missing_sequences - set(retrieved)
        if still_missing:
            print('Still missing:'+"".join(f' {p}\n' for p in still_missing))
        return retrieved
