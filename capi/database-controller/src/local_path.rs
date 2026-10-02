//! Linux-only, fd-relative management of the local CNPG volume subtree.

use std::{
    ffi::{CStr, CString},
    fmt, io,
    os::{
        fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd, RawFd},
        unix::ffi::OsStrExt,
    },
    path::{Component, Path, PathBuf},
};

const POSTGRES_UID: libc::uid_t = 26;
const POSTGRES_GID: libc::gid_t = 26;

fn database_owner() -> (libc::uid_t, libc::gid_t) {
    if cfg!(test) {
        (unsafe { libc::geteuid() }, unsafe { libc::getegid() })
    } else {
        (POSTGRES_UID, POSTGRES_GID)
    }
}

#[derive(Debug)]
pub enum PathError {
    InvalidInput(&'static str),
    MissingRoot,
    MissingPath,
    UnsafeEntry,
    Changed,
    Io(io::Error),
}

impl fmt::Display for PathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidInput(reason) => write!(f, "invalid local volume path: {reason}"),
            Self::MissingRoot => write!(f, "local volume root does not exist"),
            Self::MissingPath => write!(f, "local volume parent does not exist"),
            Self::UnsafeEntry => write!(f, "local volume contains a symlink or foreign entry"),
            Self::Changed => write!(f, "local volume path changed during operation"),
            Self::Io(error) => write!(f, "local volume I/O error: {error}"),
        }
    }
}

impl std::error::Error for PathError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct Identity {
    device: libc::dev_t,
    inode: libc::ino_t,
}

impl Identity {
    fn of(stat: &libc::stat) -> Self {
        Self {
            device: stat.st_dev,
            inode: stat.st_ino,
        }
    }
}

struct Node {
    name: CString,
    fd: OwnedFd,
    identity: Identity,
}

fn stat_fd(fd: RawFd) -> Result<libc::stat, PathError> {
    // SAFETY: fstat initializes the stat on success, and fd is held open by the caller.
    unsafe {
        let mut stat = std::mem::MaybeUninit::uninit();
        if libc::fstat(fd, stat.as_mut_ptr()) != 0 {
            return Err(PathError::Io(io::Error::last_os_error()));
        }
        Ok(stat.assume_init())
    }
}

fn stat_entry(fd: RawFd, name: &CStr) -> Result<libc::stat, PathError> {
    // SAFETY: name is NUL-terminated and the output is read only on success.
    unsafe {
        let mut stat = std::mem::MaybeUninit::uninit();
        if libc::fstatat(
            fd,
            name.as_ptr(),
            stat.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        ) != 0
        {
            return Err(PathError::Io(io::Error::last_os_error()));
        }
        Ok(stat.assume_init())
    }
}

fn open_at(fd: RawFd, name: &CStr, flags: libc::c_int) -> Result<OwnedFd, PathError> {
    // SAFETY: name is NUL-terminated; ownership of the returned descriptor is transferred.
    let opened = unsafe {
        libc::openat(
            fd,
            name.as_ptr(),
            flags | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        )
    };
    if opened < 0 {
        let error = io::Error::last_os_error();
        return Err(match error.raw_os_error() {
            Some(libc::ELOOP | libc::ENOTDIR) => PathError::UnsafeEntry,
            _ => PathError::Io(error),
        });
    }
    // SAFETY: openat returned a new, exclusively owned descriptor.
    Ok(unsafe { OwnedFd::from_raw_fd(opened) })
}

fn directory(fd: RawFd, name: &CStr) -> Result<OwnedFd, PathError> {
    open_at(
        fd,
        name,
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NONBLOCK,
    )
}

fn append(chain: &mut Vec<Node>, name: CString, fd: OwnedFd) -> Result<(), PathError> {
    if chain.len() >= 256 {
        return Err(PathError::UnsafeEntry);
    }
    let stat = stat_fd(fd.as_raw_fd())?;
    if stat.st_mode & libc::S_IFMT != libc::S_IFDIR {
        return Err(PathError::UnsafeEntry);
    }
    let parent = chain.last().expect("absolute root is open");
    if Identity::of(&stat) != Identity::of(&stat_entry(parent.fd.as_raw_fd(), &name)?) {
        return Err(PathError::Changed);
    }
    chain.push(Node {
        name,
        fd,
        identity: Identity::of(&stat),
    });
    Ok(())
}

fn verify(chain: &[Node]) -> Result<(), PathError> {
    let slash = CString::new("/").expect("static path");
    let root = directory(libc::AT_FDCWD, &slash)?;
    if Identity::of(&stat_fd(root.as_raw_fd())?) != chain[0].identity {
        return Err(PathError::Changed);
    }
    let mut current = root;
    for node in chain.iter().skip(1) {
        let stat = stat_entry(current.as_raw_fd(), &node.name).map_err(|_| PathError::Changed)?;
        if stat.st_mode & libc::S_IFMT != libc::S_IFDIR || Identity::of(&stat) != node.identity {
            return Err(PathError::Changed);
        }
        let next = directory(current.as_raw_fd(), &node.name).map_err(|_| PathError::Changed)?;
        if Identity::of(&stat_fd(next.as_raw_fd())?) != node.identity {
            return Err(PathError::Changed);
        }
        current = next;
    }
    Ok(())
}

fn root_chain(volume_root: &Path) -> Result<Vec<Node>, PathError> {
    if !volume_root.is_absolute() || volume_root == Path::new("/") {
        return Err(PathError::InvalidInput(
            "root must be an absolute volume mountpoint",
        ));
    }
    let mut normalized = PathBuf::new();
    for component in volume_root.components() {
        match component {
            Component::RootDir | Component::Normal(_) => normalized.push(component.as_os_str()),
            _ => return Err(PathError::InvalidInput("root must have no dot components")),
        }
    }
    if normalized.as_os_str().as_bytes() != volume_root.as_os_str().as_bytes() {
        return Err(PathError::InvalidInput("root must have canonical spelling"));
    }
    let slash = CString::new("/").expect("static path");
    let fd = directory(libc::AT_FDCWD, &slash)?;
    let identity = Identity::of(&stat_fd(fd.as_raw_fd())?);
    let mut chain = vec![Node {
        name: slash,
        fd,
        identity,
    }];
    for component in volume_root.components().skip(1) {
        let name = CString::new(component.as_os_str().as_bytes())
            .map_err(|_| PathError::InvalidInput("root contains NUL"))?;
        let fd = match directory(chain.last().expect("root").fd.as_raw_fd(), &name) {
            Err(PathError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {
                return Err(PathError::MissingRoot);
            }
            other => other?,
        };
        append(&mut chain, name, fd)?;
    }
    verify(&chain)?;
    Ok(chain)
}

fn uid(value: &str) -> Result<&str, PathError> {
    let bytes = value.as_bytes();
    if bytes.len() != 36
        || bytes.iter().enumerate().any(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                *byte != b'-'
            } else {
                !matches!(byte, b'0'..=b'9' | b'a'..=b'f')
            }
        })
    {
        return Err(PathError::InvalidInput("UID must be a lowercase UUID"));
    }
    Ok(value)
}

fn names(catalog_uid: &str, logical_uid: &str, ordinal: i32) -> Result<[CString; 5], PathError> {
    let catalog_uid = uid(catalog_uid)?;
    let logical_uid = uid(logical_uid)?;
    if !(1..=3).contains(&ordinal) {
        return Err(PathError::InvalidInput("ordinal must be 1, 2, or 3"));
    }
    Ok([
        CString::new("volumes").expect("static component"),
        CString::new("cnpg").expect("static component"),
        CString::new(catalog_uid).expect("validated UID"),
        CString::new(logical_uid).expect("validated UID"),
        CString::new(ordinal.to_string()).expect("validated ordinal"),
    ])
}

fn ownership(node: &Node, created: bool) -> Result<(), PathError> {
    let attr = c"user.cnpg_vcluster.local_path";
    let stamp = format!(
        "v1:{}:{}:{}",
        node.name.to_string_lossy(),
        node.identity.device,
        node.identity.inode
    );
    let fd = node.fd.as_raw_fd();
    if created {
        // SAFETY: the attribute and value remain valid for the duration of fsetxattr.
        if unsafe {
            libc::fsetxattr(
                fd,
                attr.as_ptr(),
                stamp.as_ptr().cast(),
                stamp.len(),
                libc::XATTR_CREATE,
            )
        } != 0
        {
            return Err(PathError::Io(io::Error::last_os_error()));
        }
    }
    let mut stored = vec![0u8; stamp.len() + 1];
    // SAFETY: stored has capacity for the requested length, and fd remains open.
    let size =
        unsafe { libc::fgetxattr(fd, attr.as_ptr(), stored.as_mut_ptr().cast(), stored.len()) };
    if size < 0 {
        let error = io::Error::last_os_error();
        return Err(match error.raw_os_error() {
            Some(libc::ENODATA | libc::ERANGE) => PathError::UnsafeEntry,
            _ => PathError::Io(error),
        });
    }
    if stored[..size as usize] != *stamp.as_bytes() {
        return Err(PathError::UnsafeEntry);
    }
    Ok(())
}

fn mkdir(chain: &mut Vec<Node>, name: &CStr, depth: usize) -> Result<(), PathError> {
    verify(chain)?;
    let parent = chain.last().expect("root");
    let mode = if depth == 4 { 0o700 } else { 0o711 };
    // SAFETY: name is NUL-terminated; mkdirat acts only within the open parent.
    let created = unsafe { libc::mkdirat(parent.fd.as_raw_fd(), name.as_ptr(), mode) } == 0;
    if !created {
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::AlreadyExists {
            return Err(PathError::Io(error));
        }
    }
    let fd = directory(parent.fd.as_raw_fd(), name)?;
    append(chain, name.to_owned(), fd)?;
    let current = chain.last().expect("created directory");
    let stat = stat_fd(current.fd.as_raw_fd())?;
    let (postgres_uid, postgres_gid) = database_owner();
    let expected_uid = if depth == 4 {
        postgres_uid
    } else {
        unsafe { libc::geteuid() }
    };
    if !created && stat.st_uid != expected_uid {
        return Err(PathError::UnsafeEntry);
    }
    if created && unsafe { libc::fchmod(current.fd.as_raw_fd(), mode) } != 0 {
        return Err(PathError::Io(io::Error::last_os_error()));
    }
    if depth >= 2 {
        ownership(chain.last().expect("owned directory"), created)?;
    }
    if created && depth == 4 {
        // SAFETY: only the newly created ordinal is transferred to PostgreSQL.
        if unsafe { libc::fchown(current.fd.as_raw_fd(), postgres_uid, postgres_gid) } != 0 {
            return Err(PathError::Io(io::Error::last_os_error()));
        }
    }
    let stat = stat_fd(current.fd.as_raw_fd())?;
    if stat.st_uid != expected_uid || stat.st_mode & 0o777 != mode {
        return Err(PathError::UnsafeEntry);
    }
    verify(chain)
}

fn existing(chain: &mut Vec<Node>, name: &CStr, depth: usize) -> Result<bool, PathError> {
    verify(chain)?;
    let parent = chain.last().expect("root");
    let fd = match directory(parent.fd.as_raw_fd(), name) {
        Err(PathError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {
            verify(chain)?;
            return Ok(false);
        }
        other => other?,
    };
    append(chain, name.to_owned(), fd)?;
    let stat = stat_fd(chain.last().expect("directory").fd.as_raw_fd())?;
    let expected_uid = if depth == 4 {
        database_owner().0
    } else {
        unsafe { libc::geteuid() }
    };
    let expected_mode = if depth == 4 { 0o700 } else { 0o711 };
    if stat.st_uid != expected_uid || stat.st_mode & 0o777 != expected_mode {
        return Err(PathError::UnsafeEntry);
    }
    if depth >= 2 {
        ownership(chain.last().expect("owned directory"), false)?;
    }
    verify(chain)?;
    Ok(true)
}

struct Entries(*mut libc::DIR);

impl Drop for Entries {
    fn drop(&mut self) {
        // SAFETY: fdopendir transferred ownership of the descriptor to this DIR.
        unsafe { libc::closedir(self.0) };
    }
}

fn entries(fd: RawFd) -> Result<Vec<CString>, PathError> {
    // Reopen "." instead of dup: duplicated directory fds share a read offset.
    let dot = CString::new(".").expect("static component");
    let stream_fd = directory(fd, &dot)?.into_raw_fd();
    // SAFETY: fdopendir takes ownership of this independently opened directory fd.
    let stream = unsafe { libc::fdopendir(stream_fd) };
    if stream.is_null() {
        let error = io::Error::last_os_error();
        // SAFETY: fdopendir did not take ownership on failure.
        unsafe { libc::close(stream_fd) };
        return Err(PathError::Io(error));
    }
    let stream = Entries(stream);
    let mut result = Vec::new();
    loop {
        // SAFETY: Linux errno and DIR are thread-local / exclusively owned here.
        let entry = unsafe {
            *libc::__errno_location() = 0;
            libc::readdir(stream.0)
        };
        if entry.is_null() {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(0) {
                return Err(PathError::Io(error));
            }
            break;
        }
        // SAFETY: readdir supplies a NUL-terminated d_name valid until the next call.
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
        if name.to_bytes() != b"." && name.to_bytes() != b".." {
            result.push(name.to_owned());
        }
    }
    result.sort();
    Ok(result)
}

fn kind(stat: &libc::stat) -> Result<libc::mode_t, PathError> {
    let kind = stat.st_mode & libc::S_IFMT;
    if kind != libc::S_IFREG && kind != libc::S_IFDIR {
        return Err(PathError::UnsafeEntry);
    }
    Ok(kind)
}

fn check_tree(chain: &mut Vec<Node>) -> Result<(), PathError> {
    verify(chain)?;
    let fd = chain.last().expect("directory").fd.as_raw_fd();
    for name in entries(fd)? {
        let stat = stat_entry(fd, &name)?;
        if kind(&stat)? == libc::S_IFDIR {
            let child = directory(fd, &name)?;
            if Identity::of(&stat_fd(child.as_raw_fd())?) != Identity::of(&stat) {
                return Err(PathError::Changed);
            }
            append(chain, name, child)?;
            check_tree(chain)?;
            chain.pop();
        }
    }
    verify(chain)
}

fn unlink(
    chain: &[Node],
    name: &CStr,
    identity: Identity,
    flags: libc::c_int,
) -> Result<(), PathError> {
    verify(chain)?;
    let parent = chain.last().expect("parent").fd.as_raw_fd();
    if Identity::of(&stat_entry(parent, name)?) != identity {
        return Err(PathError::Changed);
    }
    // SAFETY: name is NUL-terminated and removal is relative to a verified open parent.
    if unsafe { libc::unlinkat(parent, name.as_ptr(), flags) } != 0 {
        return Err(PathError::Io(io::Error::last_os_error()));
    }
    verify(chain)
}

fn delete_tree(chain: &mut Vec<Node>) -> Result<(), PathError> {
    verify(chain)?;
    let fd = chain.last().expect("directory").fd.as_raw_fd();
    for name in entries(fd)? {
        let stat = stat_entry(fd, &name)?;
        let identity = Identity::of(&stat);
        if kind(&stat)? == libc::S_IFDIR {
            let child = directory(fd, &name)?;
            if Identity::of(&stat_fd(child.as_raw_fd())?) != identity {
                return Err(PathError::Changed);
            }
            append(chain, name.clone(), child)?;
            delete_tree(chain)?;
            chain.pop();
            unlink(chain, &name, identity, libc::AT_REMOVEDIR)?;
        } else {
            let child = open_at(fd, &name, libc::O_PATH)?;
            if Identity::of(&stat_fd(child.as_raw_fd())?) != identity {
                return Err(PathError::Changed);
            }
            unlink(chain, &name, identity, 0)?;
        }
    }
    verify(chain)
}

/// Create only the catalog/logical-instance/ordinal subtree inside an existing volume root.
///
/// The backing filesystem must support `user.*` extended attributes for ownership stamps.
pub fn prepare(
    volume_root: &Path,
    catalog_uid: &str,
    logical_uid: &str,
    ordinal: i32,
) -> Result<PathBuf, PathError> {
    let components = names(catalog_uid, logical_uid, ordinal)?;
    let mut chain = root_chain(volume_root)?;
    for (index, name) in components.iter().enumerate() {
        mkdir(&mut chain, name, index)?;
    }
    Ok(volume_root
        .join("volumes/cnpg")
        .join(catalog_uid)
        .join(logical_uid)
        .join(ordinal.to_string()))
}

/// Inspect an ordinal without creating it; all existing UID components must be stamped.
pub fn inspect(
    volume_root: &Path,
    catalog_uid: &str,
    logical_uid: &str,
    ordinal: i32,
) -> Result<bool, PathError> {
    let components = names(catalog_uid, logical_uid, ordinal)?;
    let mut chain = root_chain(volume_root)?;
    for (index, name) in components.iter().enumerate() {
        if !existing(&mut chain, name, index)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// A finalized entry must have no residual subtree, even outside known ordinals.
pub fn entry_absent(
    volume_root: &Path,
    catalog_uid: &str,
    logical_uid: &str,
) -> Result<bool, PathError> {
    let components = names(catalog_uid, logical_uid, 1)?;
    let mut chain = root_chain(volume_root)?;
    for (index, name) in components[..4].iter().enumerate() {
        if !existing(&mut chain, name, index)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Remove one ordinal, then prune its empty logical and catalog UID directories.
///
/// An absent ordinal is idempotent only when its existing logical-instance parent was verified.
pub fn remove(
    volume_root: &Path,
    catalog_uid: &str,
    logical_uid: &str,
    ordinal: i32,
) -> Result<(), PathError> {
    let components = names(catalog_uid, logical_uid, ordinal)?;
    let mut chain = root_chain(volume_root)?;
    for (index, name) in components[..4].iter().enumerate() {
        if !existing(&mut chain, name, index)? {
            return Err(PathError::MissingPath);
        }
    }
    if !existing(&mut chain, &components[4], 4)? {
        return Ok(());
    }
    check_tree(&mut chain)?;
    delete_tree(&mut chain)?;
    for depth in (3..=5).rev() {
        let node = chain.pop().expect("owned directory");
        let parent = chain.last().expect("volume root remains");
        verify(&chain)?;
        if Identity::of(&stat_entry(parent.fd.as_raw_fd(), &node.name)?) != node.identity {
            return Err(PathError::Changed);
        }
        // SAFETY: the name and open parent refer to a verified directory.
        let result = unsafe {
            libc::unlinkat(
                parent.fd.as_raw_fd(),
                node.name.as_ptr(),
                libc::AT_REMOVEDIR,
            )
        };
        if result != 0 {
            let error = io::Error::last_os_error();
            if depth < 5 && error.raw_os_error() == Some(libc::ENOTEMPTY) {
                return verify(&chain);
            }
            return Err(PathError::Io(error));
        }
        verify(&chain)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        os::unix::fs::{MetadataExt, PermissionsExt, symlink},
        sync::atomic::{AtomicU64, Ordering},
    };

    const CATALOG: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const FIRST: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const SECOND: &str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
    static NEXT: AtomicU64 = AtomicU64::new(0);

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            let path = std::env::current_dir()
                .expect("working directory")
                .join("target/local-path-tests")
                .join(format!(
                    "{}-{}",
                    std::process::id(),
                    NEXT.fetch_add(1, Ordering::Relaxed)
                ));
            fs::create_dir_all(&path).expect("create isolated project-local fixture");
            Self(path)
        }

        fn root(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).expect("remove isolated test fixture");
        }
    }

    #[test]
    fn removes_only_one_ordinal_and_prunes_empty_ancestors() {
        let fixture = Fixture::new();
        let first = prepare(fixture.root(), CATALOG, FIRST, 1).unwrap();
        let sibling = prepare(fixture.root(), CATALOG, SECOND, 2).unwrap();
        fs::create_dir(first.join("data")).unwrap();
        fs::write(first.join("data/segment"), b"data").unwrap();
        fs::write(sibling.join("sibling"), b"keep").unwrap();
        assert_eq!(prepare(fixture.root(), CATALOG, FIRST, 1).unwrap(), first);
        remove(fixture.root(), CATALOG, FIRST, 1).unwrap();
        assert!(!first.exists());
        assert!(!first.parent().unwrap().exists());
        assert!(entry_absent(fixture.root(), CATALOG, FIRST).unwrap());
        assert!(!entry_absent(fixture.root(), CATALOG, SECOND).unwrap());
        assert_eq!(fs::read(sibling.join("sibling")).unwrap(), b"keep");
        assert!(matches!(
            remove(fixture.root(), CATALOG, FIRST, 1),
            Err(PathError::MissingPath)
        ));
        remove(fixture.root(), CATALOG, SECOND, 2).unwrap();
        assert!(fixture.root().is_dir());
        assert!(fixture.root().join("volumes/cnpg").is_dir());
        assert!(!fixture.root().join("volumes/cnpg").join(CATALOG).exists());
    }

    #[test]
    fn absent_ordinal_requires_verified_parent() {
        let fixture = Fixture::new();
        prepare(fixture.root(), CATALOG, FIRST, 1).unwrap();
        remove(fixture.root(), CATALOG, FIRST, 2).unwrap();
        assert!(matches!(
            remove(fixture.root(), CATALOG, SECOND, 1),
            Err(PathError::MissingPath)
        ));
    }

    #[test]
    fn data_owner_can_write_while_uid_parents_are_searchable_only() {
        let fixture = Fixture::new();
        let path = prepare(fixture.root(), CATALOG, FIRST, 1).unwrap();
        for ancestor in [
            fixture.root().join("volumes"),
            fixture.root().join("volumes/cnpg"),
            fixture.root().join("volumes/cnpg").join(CATALOG),
            path.parent().unwrap().to_path_buf(),
        ] {
            assert_eq!(
                fs::metadata(ancestor).unwrap().permissions().mode() & 0o777,
                0o711
            );
        }
        let stat = fs::metadata(&path).unwrap();
        assert_eq!(stat.uid(), database_owner().0);
        assert_eq!(stat.gid(), database_owner().1);
        assert_eq!(stat.permissions().mode() & 0o777, 0o700);
        fs::write(path.join("postgresql.conf"), b"workload write").unwrap();
        assert_eq!(
            fs::read(path.join("postgresql.conf")).unwrap(),
            b"workload write"
        );
    }

    #[test]
    fn unexpected_entry_file_blocks_terminal_absence_without_harming_siblings() {
        let fixture = Fixture::new();
        let first = prepare(fixture.root(), CATALOG, FIRST, 1).unwrap();
        let sibling = prepare(fixture.root(), CATALOG, SECOND, 1).unwrap();
        fs::write(first.parent().unwrap().join("foreign"), b"untouched").unwrap();
        remove(fixture.root(), CATALOG, FIRST, 1).unwrap();
        assert!(!entry_absent(fixture.root(), CATALOG, FIRST).unwrap());
        assert_eq!(
            fs::read(first.parent().unwrap().join("foreign")).unwrap(),
            b"untouched"
        );
        assert!(sibling.is_dir());
    }

    #[test]
    fn rejects_invalid_inputs_and_missing_or_tampered_root() {
        let fixture = Fixture::new();
        assert!(matches!(
            prepare(fixture.root(), "../bad", FIRST, 1),
            Err(PathError::InvalidInput(_))
        ));
        assert!(matches!(
            prepare(fixture.root(), CATALOG, &FIRST.to_uppercase(), 1),
            Err(PathError::InvalidInput(_))
        ));
        assert!(matches!(
            remove(fixture.root(), CATALOG, FIRST, 4),
            Err(PathError::InvalidInput(_))
        ));
        assert!(matches!(
            prepare(&fixture.root().join("missing"), CATALOG, FIRST, 1),
            Err(PathError::MissingRoot)
        ));
        assert!(matches!(
            remove(&fixture.root().join("missing"), CATALOG, FIRST, 1),
            Err(PathError::MissingRoot)
        ));
        assert!(matches!(
            prepare(&fixture.root().join("."), CATALOG, FIRST, 1),
            Err(PathError::InvalidInput(_))
        ));
        let link = fixture.root().join("root-link");
        symlink(fixture.root(), &link).unwrap();
        assert!(matches!(
            prepare(&link, CATALOG, FIRST, 1),
            Err(PathError::UnsafeEntry)
        ));
    }

    #[test]
    fn refuses_foreign_files_and_symlinks_in_owned_components() {
        for depth in 0..5 {
            let fixture = Fixture::new();
            let parts = ["volumes", "cnpg", CATALOG, FIRST, "1"];
            let parent = parts[..depth]
                .iter()
                .fold(fixture.root().to_owned(), |path, part| path.join(part));
            fs::create_dir_all(&parent).unwrap();
            let entry = parent.join(parts[depth]);
            if depth % 2 == 0 {
                fs::write(&entry, b"foreign").unwrap();
            } else {
                symlink(fixture.root(), &entry).unwrap();
            }
            assert!(matches!(
                prepare(fixture.root(), CATALOG, FIRST, 1),
                Err(PathError::UnsafeEntry)
            ));
            assert!(matches!(
                remove(fixture.root(), CATALOG, FIRST, 1),
                Err(PathError::UnsafeEntry)
            ));
            assert!(entry.symlink_metadata().is_ok());
        }
    }

    #[test]
    fn refuses_nested_symlink_without_partial_deletion() {
        let fixture = Fixture::new();
        let path = prepare(fixture.root(), CATALOG, FIRST, 1).unwrap();
        let sibling = prepare(fixture.root(), CATALOG, SECOND, 1).unwrap();
        fs::write(path.join("a-keep"), b"keep").unwrap();
        fs::create_dir(path.join("nested")).unwrap();
        symlink(&sibling, path.join("nested/link")).unwrap();
        assert!(matches!(
            remove(fixture.root(), CATALOG, FIRST, 1),
            Err(PathError::UnsafeEntry)
        ));
        assert_eq!(fs::read(path.join("a-keep")).unwrap(), b"keep");
        assert!(sibling.exists());
    }

    #[test]
    fn rejects_same_name_uid_symlink_replacement() {
        let fixture = Fixture::new();
        let path = prepare(fixture.root(), CATALOG, FIRST, 1).unwrap();
        let sibling = prepare(fixture.root(), CATALOG, SECOND, 1).unwrap();
        let logical = path.parent().unwrap();
        let moved = logical.with_file_name("moved");
        fs::rename(logical, &moved).unwrap();
        symlink(&moved, logical).unwrap();
        assert!(matches!(
            remove(fixture.root(), CATALOG, FIRST, 1),
            Err(PathError::UnsafeEntry)
        ));
        assert!(moved.join("1").is_dir());
        assert!(sibling.is_dir());
    }

    #[test]
    fn detects_same_name_directory_and_root_replacement_after_open() {
        let fixture = Fixture::new();
        let root = fixture.root().join("mount");
        fs::create_dir(&root).unwrap();
        prepare(&root, CATALOG, FIRST, 1).unwrap();
        let mut chain = root_chain(&root).unwrap();
        for (index, name) in names(CATALOG, FIRST, 1).unwrap().iter().take(4).enumerate() {
            assert!(existing(&mut chain, name, index).unwrap());
        }
        let logical = root.join("volumes/cnpg").join(CATALOG).join(FIRST);
        let moved = logical.with_file_name("old-logical");
        fs::rename(&logical, &moved).unwrap();
        fs::create_dir(&logical).unwrap();
        assert!(matches!(verify(&chain), Err(PathError::Changed)));
        assert!(matches!(
            remove(&root, CATALOG, FIRST, 1),
            Err(PathError::UnsafeEntry)
        ));

        let root_chain = root_chain(&root).unwrap();
        let old_root = fixture.root().join("old-mount");
        fs::rename(&root, &old_root).unwrap();
        fs::create_dir(&root).unwrap();
        assert!(matches!(verify(&root_chain), Err(PathError::Changed)));
    }

    #[test]
    fn preserves_ordinal_and_catalog_siblings() {
        let fixture = Fixture::new();
        let first = prepare(fixture.root(), CATALOG, FIRST, 1).unwrap();
        let second = prepare(fixture.root(), CATALOG, FIRST, 2).unwrap();
        let other_catalog = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
        let third = prepare(fixture.root(), other_catalog, FIRST, 1).unwrap();
        fs::write(second.join("data"), b"second").unwrap();
        fs::write(third.join("data"), b"third").unwrap();
        fs::write(first.join("data"), b"first").unwrap();
        remove(fixture.root(), CATALOG, FIRST, 1).unwrap();
        assert_eq!(fs::read(second.join("data")).unwrap(), b"second");
        assert_eq!(fs::read(third.join("data")).unwrap(), b"third");
        assert!(first.parent().unwrap().exists());
    }

    #[test]
    fn rejects_same_name_ordinal_directory_replacement() {
        let fixture = Fixture::new();
        let path = prepare(fixture.root(), CATALOG, FIRST, 1).unwrap();
        let sibling = prepare(fixture.root(), CATALOG, FIRST, 2).unwrap();
        let moved = path.with_file_name("old-ordinal");
        fs::rename(&path, &moved).unwrap();
        fs::create_dir(&path).unwrap();
        fs::write(path.join("foreign"), b"keep").unwrap();
        assert!(matches!(
            remove(fixture.root(), CATALOG, FIRST, 1),
            Err(PathError::UnsafeEntry)
        ));
        assert!(path.join("foreign").exists());
        assert!(moved.is_dir());
        assert!(sibling.is_dir());
    }
}
