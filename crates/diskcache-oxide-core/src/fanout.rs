//! The on-disk format of a `FanoutCache` root: the shard hash and the root manifest (plan 13).
//!
//! Both are frozen the moment they ship. Every process must route a key to the same shard forever,
//! and two processes creating one root at the same moment must agree on a single manifest. This
//! module only deals in the hash and the manifest file; shard databases are the store's business.
//!
//! **Hash:** `xxh3-64`, seed 0, over one byte (the key's `key_type`) followed by the key's
//! canonical bytes, mapped to a shard by multiply-high. Never `DefaultHasher` (its algorithm isn't
//! promised) and never Python's `hash()` (randomized per process).
//!
//! **Manifest:** four ASCII lines, compared byte for byte against the canonical rendering. It is
//! created by writing a private temporary file and hard-linking it to its final name, which fails
//! if another creator won (`rename` would replace the winner's file).

use std::fs::{File, OpenOptions};
use std::io::{ErrorKind, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};

use xxhash_rust::xxh3::Xxh3;

use crate::error::{Error, Result};
use crate::open::ensure_directory;

/// The manifest's file name inside a root directory.
pub const MANIFEST_FILENAME: &str = "diskcache-oxide-fanout";

/// The manifest format version.
pub const MANIFEST_FORMAT: i64 = 1;

/// The hash identity line of the manifest. A new scheme gets a new identity and a new format,
/// never a change in place.
pub const HASH_IDENTITY: &str = "xxh3-64 seed=0 range=mulhi";

/// The fewest shards a root can have.
pub const MIN_SHARDS: u32 = 1;

/// The most shards a root can have.
pub const MAX_SHARDS: u32 = 256;

/// The shard count of a new root when the caller doesn't ask for one.
pub const DEFAULT_SHARDS: u32 = 8;

/// Openers read at most this many bytes of a manifest, so a huge or hostile file costs nothing.
const MAX_MANIFEST_BYTES: u64 = 4096;

/// The hash of `key` (its canonical bytes) under `key_type`: xxh3-64 with seed 0 over the one-byte
/// type tag then the key. The tag keeps types disjoint, so the string `"1"` and the integer 1 never
/// collide by construction. Streamed, so nothing is allocated.
#[must_use]
pub fn shard_hash(key_type: u8, key: &[u8]) -> u64 {
    let mut hasher = Xxh3::with_seed(0);
    hasher.update(&[key_type]);
    hasher.update(key);
    hasher.digest()
}

/// Maps a hash to a shard index below `shards` by multiply-high: uses every bit, no division.
///
/// `shards` is a count of at least 1 (a [`Manifest`] never holds fewer).
#[must_use]
pub fn shard_index(hash: u64, shards: u32) -> usize {
    let scaled = (u128::from(hash) * u128::from(shards)) >> 64;
    // `scaled < shards <= u32::MAX`, so it fits.
    usize::try_from(scaled).unwrap_or(usize::MAX)
}

/// The shard of a key: [`shard_index`] of [`shard_hash`].
#[must_use]
pub fn shard_of(key_type: u8, key: &[u8], shards: u32) -> usize {
    shard_index(shard_hash(key_type, key), shards)
}

/// The type tag of a `str` key, as [`shard_hash`] takes it: the stored `key_type`.
const KEY_TAG_STR: u8 = 1;
const _: () = assert!(KEY_TAG_STR as i64 == crate::schema::KEY_TYPE_STR);

/// The shard of a `str` key: its UTF-8 bytes under the `str` type tag.
#[must_use]
pub fn shard_of_str(key: &str, shards: u32) -> usize {
    shard_of(KEY_TAG_STR, key.as_bytes(), shards)
}

/// The type tag of a typed key: its stored `key_type`.
const KEY_TAG_TYPED: u8 = 2;
const _: () = assert!(KEY_TAG_TYPED as i64 == crate::schema::KEY_TYPE_TYPED);

/// The shard of any key: its stored bytes under its `key_type`. A `str` key's is [`shard_of_str`]'s.
#[must_use]
pub fn shard_of_key(key: crate::key::Key<'_>, shards: u32) -> usize {
    let tag = match key {
        crate::key::Key::Str(_) => KEY_TAG_STR,
        crate::key::Key::Typed(_) => KEY_TAG_TYPED,
    };
    shard_of(tag, key.bytes(), shards)
}

/// A validated root manifest. The shard count is always in `MIN_SHARDS..=MAX_SHARDS`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Manifest {
    shards: u32,
}

impl Manifest {
    /// A manifest for `shards` shards, or `InvalidSetting` if the count is out of range.
    pub fn new(shards: u32) -> Result<Self> {
        if !(MIN_SHARDS..=MAX_SHARDS).contains(&shards) {
            return Err(Error::InvalidSetting(format!(
                "shards must be between {MIN_SHARDS} and {MAX_SHARDS}, got {shards}"
            )));
        }
        Ok(Self { shards })
    }

    /// How many shards the root has.
    #[must_use]
    pub const fn shards(self) -> u32 {
        self.shards
    }

    /// The exact bytes of the manifest file.
    #[must_use]
    pub fn render(self) -> String {
        format!(
            "diskcache-oxide fanout\nformat = {MANIFEST_FORMAT}\nshards = {}\nhash = {HASH_IDENTITY}\n",
            self.shards
        )
    }

    /// Parses the bytes of a manifest file. Lines 1 (`diskcache-oxide fanout`) and 2 (`format = N`)
    /// are frozen in every format, so a future format always reads as [`Error::FormatMismatch`] and
    /// never as invalid. Only the canonical rendering of some valid shard count
    /// is accepted; anything else is [`Error::ManifestInvalid`], or [`Error::FormatMismatch`] when
    /// it is otherwise well formed but declares another `format`.
    pub fn parse(bytes: &[u8], path: &Path) -> Result<Self> {
        let invalid = |reason: &str| Error::ManifestInvalid { path: path.to_path_buf(), reason: reason.to_owned() };
        let text = std::str::from_utf8(bytes).map_err(|_| invalid("not valid UTF-8"))?;
        let mut lines = text.split('\n');
        if lines.next() != Some("diskcache-oxide fanout") {
            return Err(invalid("not a diskcache-oxide fan-out manifest"));
        }
        let format = lines.next().and_then(|l| l.strip_prefix("format = ")).ok_or_else(|| invalid("no format line"))?;
        let format = parse_decimal(format).ok_or_else(|| invalid("the format is not a number"))?;
        if i64::from(format) != MANIFEST_FORMAT {
            return Err(Error::FormatMismatch {
                found: i64::from(format),
                expected: MANIFEST_FORMAT,
                path: Some(path.to_path_buf()),
            });
        }
        let shards = lines.next().and_then(|l| l.strip_prefix("shards = ")).ok_or_else(|| invalid("no shards line"))?;
        let shards = parse_decimal(shards).ok_or_else(|| invalid("the shard count is not a number"))?;
        let manifest = Self::new(shards).map_err(|_| invalid("the shard count is out of range"))?;
        if text != manifest.render() {
            return Err(invalid("not the canonical manifest (extra, missing or altered lines or whitespace)"));
        }
        Ok(manifest)
    }
}

/// Plain ASCII digits only: no sign, no whitespace, no leading zeros, at most 9 digits.
pub(crate) fn parse_decimal(text: &str) -> Option<u32> {
    let bytes = text.as_bytes();
    let well_formed = !bytes.is_empty()
        && bytes.len() <= 9
        && bytes.iter().all(u8::is_ascii_digit)
        && (bytes.len() == 1 || bytes[0] != b'0');
    if well_formed { text.parse().ok() } else { None }
}

/// The manifest's path inside `root`.
#[must_use]
pub fn manifest_path(root: &Path) -> PathBuf {
    root.join(MANIFEST_FILENAME)
}

/// The shard directory name for index `index`: three digits, zero padded, as stock diskcache does.
#[must_use]
pub fn shard_dirname(index: usize) -> String {
    format!("{index:03}")
}

/// Reads and validates the manifest in `root`: `Ok(None)` if there is none. Reads at most 4 KiB.
pub fn read_manifest(root: &Path) -> Result<Option<Manifest>> {
    let path = manifest_path(root);
    let mut file = match File::open(&path) {
        Ok(file) => file,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(None),
        Err(source) => return Err(Error::Io { path, source }),
    };
    let mut bytes = Vec::new();
    // One byte past the limit tells "exactly the limit" from "too big".
    (&mut file)
        .take(MAX_MANIFEST_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|source| Error::Io { path: path.clone(), source })?;
    if bytes.len() as u64 > MAX_MANIFEST_BYTES {
        return Err(Error::ManifestInvalid { path, reason: format!("larger than {MAX_MANIFEST_BYTES} bytes") });
    }
    Manifest::parse(&bytes, &path).map(Some)
}

/// The shard count a `FanoutCache` open asks for. Every variant agrees on a new root's count with
/// any concurrent creator: the first `link` wins, whatever each asked for (plan 15).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShardRequest {
    /// The stored count, or [`DEFAULT_SHARDS`] for a new root (`shards=None`).
    Stored,
    /// Exactly this count: a new root gets it, and an existing root with another count is
    /// [`Error::ShardCountMismatch`] (`shards=n`).
    Exactly(Manifest),
    /// This count for a new root, the stored count for an existing one, so never a count mismatch.
    /// The `Caches` registry's open: a root created one way runs that way until it's deleted.
    ForNewRoot(Manifest),
}

impl ShardRequest {
    /// The manifest a new root is created with.
    fn for_a_new_root(self) -> Result<Manifest> {
        match self {
            Self::Stored => Manifest::new(DEFAULT_SHARDS),
            Self::Exactly(manifest) | Self::ForNewRoot(manifest) => Ok(manifest),
        }
    }
}

/// Opens the root `root` for a `FanoutCache`: creates the directory and the manifest if there is
/// none, otherwise reads and validates it. Returns the manifest in force, as `request` decides
/// (see [`ShardRequest`]); only [`ShardRequest::Exactly`] can give [`Error::ShardCountMismatch`].
/// When this returns, the manifest is durable (the root directory is `fsync`ed), so the caller may
/// create shards. Concurrent creators (threads or processes) agree on one manifest: the first
/// `link` wins, and the comparison runs only after it, so a lost race is decided by the winner's.
pub fn open_manifest(root: &Path, request: ShardRequest) -> Result<Manifest> {
    ensure_directory(root)?;
    // Before the manifest can be written, and again once it is there: of a plain `Cache` and a
    // `FanoutCache` creating one root at once, at least one is refused, and nothing is deleted.
    refuse_plain_cache(root)?;
    let stored = open_or_create(root, request)?;
    refuse_plain_cache(root)?;
    Ok(stored)
}

/// [`open_manifest`] after its first check for a plain cache.
fn open_or_create(root: &Path, request: ShardRequest) -> Result<Manifest> {
    let stored = match read_manifest(root)? {
        Some(manifest) => manifest,
        None => match create_manifest(root, request.for_a_new_root()?)? {
            Created::Won(manifest) => return Ok(manifest),
            Created::Lost => read_manifest(root)?.ok_or_else(|| Error::ManifestInvalid {
                path: manifest_path(root),
                reason: "the manifest vanished while it was being created".to_owned(),
            })?,
        },
    };
    if let ShardRequest::Exactly(requested) = request
        && requested != stored
    {
        return Err(Error::ShardCountMismatch { stored: stored.shards, requested: requested.shards });
    }
    Ok(stored)
}

/// Refuses a plain `Cache` on a `FanoutCache` root: `directory` holds a manifest. Only a `stat`,
/// so it writes nothing (D-019).
pub(crate) fn refuse_manifest(directory: &Path) -> Result<()> {
    let path = manifest_path(directory);
    if exists(&path)? {
        return Err(Error::RoleMismatch {
            path,
            reason: "this directory is a FanoutCache root; open it with FanoutCache, not Cache".to_owned(),
        });
    }
    Ok(())
}

/// Refuses a `FanoutCache` on a directory that holds a plain `Cache`'s database. Only a `stat`.
fn refuse_plain_cache(root: &Path) -> Result<()> {
    let path = crate::open::db_path(root);
    if exists(&path)? {
        return Err(Error::RoleMismatch {
            path,
            reason: "this directory holds a plain Cache; a FanoutCache needs a directory of its own".to_owned(),
        });
    }
    Ok(())
}

/// Whether `path` exists. Any other `stat` failure is an error, never "absent".
fn exists(path: &Path) -> Result<bool> {
    path.try_exists().map_err(|source| Error::Io { path: path.to_path_buf(), source })
}

/// Who won the manifest's creation.
enum Created {
    /// This call linked the manifest into place.
    Won(Manifest),
    /// Another creator's manifest was already there.
    Lost,
}

/// Writes `manifest` to a private temporary file, `fsync`s it, hard-links it to the final name (which
/// fails if one exists) and `fsync`s the root, so a crash can never leave shards without a manifest.
fn create_manifest(root: &Path, manifest: Manifest) -> Result<Created> {
    let final_path = manifest_path(root);
    let (temp_path, mut temp) = create_temp(root)?;
    let io = |source| Error::Io { path: temp_path.clone(), source };
    // Write and fsync errors are reported against the temporary file and are never the hard-link limit.
    if let Err(e) = temp.write_all(manifest.render().as_bytes()).and_then(|()| temp.sync_all()) {
        let _ = std::fs::remove_file(&temp_path);
        return Err(io(e));
    }
    let linked = std::fs::hard_link(&temp_path, &final_path);
    // The temporary name is only a vehicle: remove it however the link went. A failed unlink leaves
    // a stray temporary file, which is documented as harmless.
    let _ = std::fs::remove_file(&temp_path);
    match linked {
        Ok(()) => {
            sync_directory(root)?;
            Ok(Created::Won(manifest))
        }
        Err(e) if e.kind() == ErrorKind::AlreadyExists => {
            // The winner `fsync`s too, but this caller is about to build shards on its manifest.
            sync_directory(root)?;
            Ok(Created::Lost)
        }
        Err(e) => Err(link_error(&final_path, e)),
    }
}

/// The error for a failed `link`: a filesystem without hard links (FAT, exFAT, SMB, some FUSE) gets
/// a message naming the limit, since there is no fallback.
fn link_error(path: &Path, source: std::io::Error) -> Error {
    if crate::os_error::is_link_unsupported(&source) {
        let source = std::io::Error::new(
            source.kind(),
            "this filesystem doesn't support hard links, which FanoutCache needs to create its manifest safely",
        );
        return Error::Io { path: path.to_path_buf(), source };
    }
    Error::Io { path: path.to_path_buf(), source }
}

/// Creates `diskcache-oxide-fanout.tmp-<pid>-<nonce>` with `O_EXCL` and, on Unix, mode 0644 (masked by the umask, like the databases; the hard link makes this inode the manifest).
fn create_temp(root: &Path) -> Result<(PathBuf, File)> {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o644);
    loop {
        let nonce = NEXT.fetch_add(1, Ordering::Relaxed);
        let path = root.join(format!("{MANIFEST_FILENAME}.tmp-{}-{nonce}", std::process::id()));
        match options.open(&path) {
            Ok(file) => return Ok((path, file)),
            // A leftover from a crashed process with the same pid: try the next nonce.
            Err(e) if e.kind() == ErrorKind::AlreadyExists => {}
            Err(source) => return Err(Error::Io { path, source }),
        }
    }
}

/// `fsync` of a directory, which makes a link created in it durable.
#[cfg(unix)]
fn sync_directory(dir: &Path) -> Result<()> {
    File::open(dir).and_then(|d| d.sync_all()).map_err(|source| Error::Io { path: dir.to_path_buf(), source })
}

/// Nothing on Windows: opening a directory needs `FILE_FLAG_BACKUP_SEMANTICS` (a plain open is
/// `ERROR_ACCESS_DENIED`), and flushing a directory handle isn't how a link becomes durable there,
/// since NTFS journals its metadata. The manifest's own `sync_all` still runs.
#[cfg(not(unix))]
#[expect(clippy::unnecessary_wraps, reason = "the same signature as the Unix version")]
fn sync_directory(_dir: &Path) -> Result<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;
    use std::sync::Barrier;

    use super::*;
    use crate::testutil::TempDir;

    const SHARD_COUNTS: [u32; 6] = [1, 2, 7, 8, 64, 256];

    fn long(n: usize) -> Vec<u8> {
        vec![b'k'; n]
    }

    /// (name, key, hash, shard at each of `SHARD_COUNTS`). The hashes were computed independently
    /// with the Python `xxhash` binding. A changed vector is a format break, never a test update.
    fn golden() -> Vec<(&'static str, Vec<u8>, u64, [usize; 6])> {
        vec![
            ("empty", Vec::new(), 0xe12e_f9d2_eb86_ceeb, [0, 1, 6, 7, 56, 225]),
            ("a", b"a".to_vec(), 0x21c8_3245_4304_9a44, [0, 0, 0, 1, 8, 33]),
            ("16 bytes", b"0123456789abcdef".to_vec(), 0x0b8b_7132_1597_c08e, [0, 0, 0, 0, 2, 11]),
            ("128 bytes", long(128), 0xf8fa_13e9_a5c3_e5c7, [0, 1, 6, 7, 62, 248]),
            ("240 bytes", long(240), 0x7222_48cc_57c9_97ec, [0, 0, 3, 3, 28, 114]),
            ("241 bytes", long(241), 0xa6f2_62b9_1a04_b20f, [0, 1, 4, 5, 41, 166]),
            ("239 bytes", long(239), 0xae00_e6db_4aef_786b, [0, 1, 4, 5, 43, 174]),
            ("nul", vec![0], 0xab94_ac0a_ce72_76c0, [0, 1, 4, 5, 42, 171]),
            ("non-BMP", "\u{1F40D}\u{10FFFF}é".as_bytes().to_vec(), 0x3051_29dc_82d8_543d, [0, 0, 1, 1, 12, 48]),
        ]
    }

    #[test]
    fn golden_vectors_are_pinned() {
        for (name, key, hash, shards) in golden() {
            assert_eq!(shard_hash(1, &key), hash, "{name}");
            for (count, expected) in SHARD_COUNTS.iter().zip(shards) {
                assert_eq!(shard_of(1, &key, *count), expected, "{name} at {count} shards");
            }
        }
    }

    #[test]
    fn the_type_tag_keeps_key_types_apart() {
        assert_eq!(shard_hash(1, b"a"), 0x21c8_3245_4304_9a44);
        assert_eq!(shard_hash(2, b"a"), 0x9134_6750_400e_332e);
    }

    #[test]
    fn xxh3_matches_its_published_empty_input_value() {
        // XXH3_64bits("") with seed 0, from xxHash's own sanity checks: the crate can't become the format.
        assert_eq!(xxhash_rust::xxh3::xxh3_64_with_seed(b"", 0), 0x2d06_8005_38d3_94c2);
    }

    /// Input of length `n`, a fixed function of `n`, so Python can regenerate it.
    fn pattern(n: usize) -> Vec<u8> {
        (0..n).map(|i| u8::try_from((i * 31 + n) & 255).unwrap()).collect()
    }

    #[test]
    fn streaming_equals_one_shot_over_lengths_0_to_2048() {
        for n in 0..=2048 {
            let input = pattern(n);
            let mut hasher = Xxh3::with_seed(0);
            // A split at a varying point exercises the buffering across xxh3's size classes.
            let (head, tail) = input.split_at(n / 3);
            hasher.update(head);
            hasher.update(tail);
            assert_eq!(hasher.digest(), xxhash_rust::xxh3::xxh3_64_with_seed(&input, 0), "length {n}");
            let mut tagged = vec![7u8];
            tagged.extend_from_slice(&input);
            assert_eq!(shard_hash(7, &input), xxhash_rust::xxh3::xxh3_64_with_seed(&tagged, 0), "tagged {n}");
        }
    }

    #[test]
    fn hash_matches_the_python_xxhash_binding_over_lengths_0_to_2048() {
        // The big-endian 64-bit hashes of `pattern(0..=2048)`, hashed again. Computed with the Python
        // `xxhash` binding (`xxh3_64_intdigest(data, seed=0)`); one number stands for 2,049 hashes.
        let mut digests = Vec::new();
        for n in 0..=2048 {
            digests.extend_from_slice(&xxhash_rust::xxh3::xxh3_64_with_seed(&pattern(n), 0).to_be_bytes());
        }
        assert_eq!(xxhash_rust::xxh3::xxh3_64_with_seed(&digests, 0), 0xe393_cf0f_4fd3_0680);
    }

    #[test]
    fn a_shared_prefix_still_spreads_across_eight_shards() {
        let prefix = "p".repeat(60);
        let mut counts = [0usize; 8];
        for i in 0..1000 {
            counts[shard_of(1, format!("{prefix}{i}").as_bytes(), 8)] += 1;
        }
        // Computed independently: [109, 126, 126, 126, 127, 129, 114, 143].
        assert_eq!(counts, [109, 126, 126, 126, 127, 129, 114, 143]);
        assert!(counts.iter().all(|&c| (100..=150).contains(&c)), "{counts:?}");
    }

    #[test]
    fn every_hash_lands_inside_the_shard_range() {
        for shards in [1, 2, 3, 255, 256] {
            for hash in [0, 1, u64::MAX / 2, u64::MAX] {
                assert!(shard_index(hash, shards) < shards as usize);
            }
        }
        assert_eq!(shard_index(u64::MAX, 256), 255);
        assert_eq!(shard_index(0, 256), 0);
    }

    const CANONICAL_8: &str = "diskcache-oxide fanout\nformat = 1\nshards = 8\nhash = xxh3-64 seed=0 range=mulhi\n";

    #[test]
    fn manifest_bytes_are_pinned() {
        assert_eq!(Manifest::new(8).unwrap().render(), CANONICAL_8);
        assert_eq!(
            Manifest::new(256).unwrap().render(),
            "diskcache-oxide fanout\nformat = 1\nshards = 256\nhash = xxh3-64 seed=0 range=mulhi\n"
        );
        assert_eq!(MANIFEST_FILENAME, "diskcache-oxide-fanout");
        assert_eq!(shard_dirname(0), "000");
        assert_eq!(shard_dirname(7), "007");
        assert_eq!(shard_dirname(255), "255");
    }

    #[test]
    fn every_valid_count_round_trips() {
        let path = Path::new("m");
        for shards in MIN_SHARDS..=MAX_SHARDS {
            let manifest = Manifest::new(shards).unwrap();
            assert_eq!(Manifest::parse(manifest.render().as_bytes(), path).unwrap(), manifest);
        }
        assert!(matches!(Manifest::new(0), Err(Error::InvalidSetting(_))));
        assert!(matches!(Manifest::new(257), Err(Error::InvalidSetting(_))));
    }

    #[test]
    fn non_canonical_manifests_are_refused() {
        let path = Path::new("m");
        let bad: Vec<(&str, String)> = vec![
            ("empty", String::new()),
            ("no trailing newline", CANONICAL_8.trim_end().to_owned()),
            ("extra trailing newline", format!("{CANONICAL_8}\n")),
            ("trailing space", CANONICAL_8.replace("shards = 8", "shards = 8 ")),
            ("leading zero", CANONICAL_8.replace("shards = 8", "shards = 08")),
            ("plus sign", CANONICAL_8.replace("shards = 8", "shards = +8")),
            ("zero shards", CANONICAL_8.replace("shards = 8", "shards = 0")),
            ("257 shards", CANONICAL_8.replace("shards = 8", "shards = 257")),
            ("huge count", CANONICAL_8.replace("shards = 8", "shards = 99999999999999999999")),
            ("CRLF", CANONICAL_8.replace('\n', "\r\n")),
            ("other hash", CANONICAL_8.replace("xxh3-64", "adler32")),
            ("extra line", format!("{CANONICAL_8}x = 1\n")),
            ("wrong title", CANONICAL_8.replace("fanout", "fan-out")),
            (
                "swapped lines",
                "diskcache-oxide fanout\nshards = 8\nformat = 1\nhash = xxh3-64 seed=0 range=mulhi\n".into(),
            ),
        ];
        for (name, text) in bad {
            let err = Manifest::parse(text.as_bytes(), path).unwrap_err();
            assert!(matches!(err, Error::ManifestInvalid { .. }), "{name}: {err:?}");
        }
        let err = Manifest::parse(&[0xff, 0xfe], path).unwrap_err();
        assert!(matches!(err, Error::ManifestInvalid { .. }), "{err:?}");
        let future = CANONICAL_8.replace("format = 1", "format = 2");
        let err = Manifest::parse(future.as_bytes(), path).unwrap_err();
        assert!(matches!(err, Error::FormatMismatch { found: 2, expected: 1, path: Some(_) }), "{err:?}");
    }

    #[test]
    fn a_new_root_gets_a_private_manifest_and_nothing_else() {
        let tmp = TempDir::new("fanout-new");
        let root = tmp.path().join("root");
        assert!(read_manifest(&root).unwrap().is_none());
        let manifest = open_manifest(&root, ShardRequest::Stored).unwrap();
        assert_eq!(manifest.shards(), DEFAULT_SHARDS);
        assert_eq!(std::fs::read_to_string(manifest_path(&root)).unwrap(), CANONICAL_8);
        #[cfg(unix)]
        {
            use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
            let mode = |p: &Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
            let umask = {
                // Read the process umask from a probe file's mode, without changing it.
                let probe = tmp.path().join("probe");
                std::fs::OpenOptions::new().write(true).create_new(true).mode(0o777).open(&probe).unwrap();
                0o777 & !mode(&probe)
            };
            assert_eq!(mode(&manifest_path(&root)), 0o644 & !umask);
        }
        crate::testutil::assert_dir_mode(&root, 0o700);
        let names: Vec<_> = std::fs::read_dir(&root).unwrap().map(|e| e.unwrap().file_name()).collect();
        assert_eq!(names, [MANIFEST_FILENAME], "the temporary file is removed");
    }

    #[test]
    fn reopening_reads_the_stored_count() {
        let tmp = TempDir::new("fanout-reopen");
        let root = tmp.path().join("root");
        assert_eq!(open_manifest(&root, exactly(64)).unwrap().shards(), 64);
        assert_eq!(open_manifest(&root, ShardRequest::Stored).unwrap().shards(), 64);
        assert_eq!(open_manifest(&root, exactly(64)).unwrap().shards(), 64);
        let err = open_manifest(&root, exactly(8)).unwrap_err();
        assert!(matches!(err, Error::ShardCountMismatch { stored: 64, requested: 8 }), "{err:?}");
        let message = err.to_string();
        assert!(message.contains("64") && message.contains('8'), "{message}");
    }

    #[test]
    fn for_a_new_root_creates_its_count_and_otherwise_keeps_the_stored_one_untouched() {
        let tmp = TempDir::new("fanout-for-new");
        let root = tmp.path().join("root");
        assert_eq!(open_manifest(&root, for_new(4)).unwrap().shards(), 4, "a new root gets the requested count");
        let path = manifest_path(&root);
        let (bytes, mtime) = (std::fs::read(&path).unwrap(), std::fs::metadata(&path).unwrap().modified().unwrap());
        for other in [8, 4, 1] {
            assert_eq!(open_manifest(&root, for_new(other)).unwrap().shards(), 4, "the stored count wins");
        }
        assert_eq!(std::fs::read(&path).unwrap(), bytes, "the manifest was rewritten (D-019)");
        assert_eq!(std::fs::metadata(&path).unwrap().modified().unwrap(), mtime);
        let names: Vec<_> = std::fs::read_dir(&root).unwrap().map(|e| e.unwrap().file_name()).collect();
        assert_eq!(names, [MANIFEST_FILENAME]);
    }

    #[test]
    fn for_a_new_root_still_refuses_a_plain_caches_directory() {
        let tmp = TempDir::new("fanout-for-new-plain");
        let root = tmp.path().join("root");
        ensure_directory(&root).unwrap();
        std::fs::write(crate::open::db_path(&root), b"").unwrap();
        assert!(matches!(open_manifest(&root, for_new(4)), Err(Error::RoleMismatch { .. })));
        assert!(read_manifest(&root).unwrap().is_none(), "nothing was created");
    }

    #[test]
    fn an_oversized_or_altered_manifest_is_refused_on_open() {
        let tmp = TempDir::new("fanout-bad");
        let root = tmp.path().join("root");
        open_manifest(&root, ShardRequest::Stored).unwrap();
        let path = manifest_path(&root);
        std::fs::write(&path, vec![b'x'; 1 << 20]).unwrap();
        let err = open_manifest(&root, ShardRequest::Stored).unwrap_err();
        assert!(matches!(&err, Error::ManifestInvalid { reason, .. } if reason.contains("larger")), "{err:?}");
        std::fs::write(&path, CANONICAL_8.replace("shards = 8", "shards = 9 ")).unwrap();
        assert!(matches!(open_manifest(&root, ShardRequest::Stored), Err(Error::ManifestInvalid { .. })));
        // Nothing was replaced by the failed opens.
        assert_eq!(std::fs::read_to_string(&path).unwrap(), CANONICAL_8.replace("shards = 8", "shards = 9 "));
    }

    #[test]
    fn a_stray_temp_file_is_ignored() {
        let tmp = TempDir::new("fanout-stray");
        let root = tmp.path().join("root");
        ensure_directory(&root).unwrap();
        std::fs::write(root.join(format!("{MANIFEST_FILENAME}.tmp-1-0")), "junk").unwrap();
        assert!(read_manifest(&root).unwrap().is_none());
        assert_eq!(open_manifest(&root, exactly(4)).unwrap().shards(), 4);
    }

    #[test]
    fn missing_hard_links_get_a_clear_error() {
        let path = Path::new("/root/diskcache-oxide-fanout");
        let codes = crate::os_error::LINK_UNSUPPORTED.iter().map(|&code| crate::os_error::from_code(code));
        for source in std::iter::once(std::io::Error::from(ErrorKind::Unsupported)).chain(codes) {
            let err = link_error(path, source);
            assert!(err.to_string().contains("doesn't support hard links"), "{err}");
            assert!(matches!(err, Error::Io { .. }));
        }
        // Other failures keep their own message.
        let err = link_error(path, std::io::Error::from(ErrorKind::NotFound));
        assert!(!err.to_string().contains("hard links"), "{err}");
    }

    #[test]
    fn racing_creators_agree_on_one_manifest() {
        const THREADS: usize = 16;
        for same_count in [true, false] {
            let tmp = TempDir::new("fanout-race");
            let root = tmp.path().join("root");
            let barrier = Barrier::new(THREADS);
            let results: Vec<Result<Manifest>> = std::thread::scope(|scope| {
                let handles: Vec<_> = (0..THREADS)
                    .map(|i| {
                        let (root, barrier) = (&root, &barrier);
                        scope.spawn(move || {
                            let shards = if same_count { 16 } else { u32::try_from(i + 1).unwrap() };
                            barrier.wait();
                            open_manifest(root, exactly(shards))
                        })
                    })
                    .collect();
                handles.into_iter().map(|h| h.join().unwrap()).collect()
            });
            let winner = read_manifest(&root).unwrap().unwrap();
            let agreed: BTreeSet<u32> = results.iter().filter_map(|r| r.as_ref().ok()).map(|m| m.shards()).collect();
            assert_eq!(agreed, BTreeSet::from([winner.shards()]), "every success reports the winner's count");
            for result in &results {
                assert!(matches!(result, Ok(_) | Err(Error::ShardCountMismatch { .. })), "{result:?}");
            }
            if same_count {
                assert!(results.iter().all(Result::is_ok));
            } else {
                assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1, "only the creator's own count matches");
            }
            let names: Vec<_> = std::fs::read_dir(&root).unwrap().map(|e| e.unwrap().file_name()).collect();
            assert_eq!(names, [MANIFEST_FILENAME], "one manifest, no temporary files");
            assert_eq!(std::fs::read_to_string(manifest_path(&root)).unwrap(), winner.render());
        }
    }

    #[test]
    fn mixed_requests_racing_on_a_new_root_all_agree_on_the_winner() {
        const THREADS: usize = 18;
        for round in 0..8 {
            let tmp = TempDir::new("fanout-mixed-race");
            let root = tmp.path().join("root");
            let barrier = Barrier::new(THREADS);
            // Thread i asks one of three ways, each with a count of its own.
            let request = |i: usize| {
                let shards = u32::try_from(i % 6 + 1).unwrap();
                match i % 3 {
                    0 => ShardRequest::Stored,
                    1 => exactly(shards),
                    _ => for_new(shards),
                }
            };
            let results: Vec<(ShardRequest, Result<Manifest>)> = std::thread::scope(|scope| {
                let handles: Vec<_> = (0..THREADS)
                    .map(|i| {
                        let (root, barrier) = (&root, &barrier);
                        scope.spawn(move || {
                            barrier.wait();
                            (request(i), open_manifest(root, request(i)))
                        })
                    })
                    .collect();
                handles.into_iter().map(|h| h.join().unwrap()).collect()
            });
            let winner = read_manifest(&root).unwrap().unwrap();
            for (request, result) in &results {
                match (request, result) {
                    (ShardRequest::Exactly(asked), Ok(got)) => assert!(asked == got && *got == winner, "round {round}"),
                    (ShardRequest::Exactly(asked), Err(Error::ShardCountMismatch { stored, requested })) => {
                        assert!(*stored == winner.shards() && *requested == asked.shards(), "round {round}");
                    }
                    (ShardRequest::Stored | ShardRequest::ForNewRoot(_), Ok(got)) => {
                        assert_eq!(*got, winner, "round {round}: a lenient request didn't get the winner");
                    }
                    other => panic!("round {round}: {other:?}"),
                }
            }
            let names: Vec<_> = std::fs::read_dir(&root).unwrap().map(|e| e.unwrap().file_name()).collect();
            assert_eq!(names, [MANIFEST_FILENAME], "round {round}: one manifest, no temporary files");
        }
    }

    fn exactly(shards: u32) -> ShardRequest {
        ShardRequest::Exactly(Manifest::new(shards).unwrap())
    }

    fn for_new(shards: u32) -> ShardRequest {
        ShardRequest::ForNewRoot(Manifest::new(shards).unwrap())
    }
}
