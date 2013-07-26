TRAC_URL = "http://trac.sagemath.org/sage_trac"

import re, hashlib, urllib2, os, sys, tempfile, traceback, time, subprocess
import pprint

from util import do_or_die, extract_version, compare_version, get_base, now_str, is_git, git_commit

def digest(s):
    """
    Computes a cryptographic hash of the string s.
    """
    return hashlib.md5(s).hexdigest()

def get_url(url):
    """
    Returns the contents of url as a string.
    """
    try:
        url = url.replace(' ', '%20')
        handle = urllib2.urlopen(url, timeout=5)
        data = handle.read()
        handle.close()
        return data
    except:
        print url
        raise

def get_patch_url(ticket, patch, raw=True):
    if raw:
        return "%s/raw-attachment/ticket/%s/%s" % (TRAC_URL, ticket, patch)
    else:
        return "%s/attachment/ticket/%s/%s" % (TRAC_URL, ticket, patch)

def get_patch(ticket, patch):
    return get_url(get_patch_url(ticket, patch))

def scrape(ticket_id, force=False, db=None):
    """
    Scrapes the trac page for ticket_id, updating the database if needed.
    """
    ticket_id = int(ticket_id)
    if ticket_id == 0:
        if db is not None:
            db_info = db.lookup_ticket(ticket_id)
            if db_info is not None:
                return db_info
        return {
            'id'            : ticket_id,
            'title'         : 'base',
            'page_hash'     : '0',
            'status'        : 'base',
            'priority'      : 'base',
            'component'     : 'base',
            'depends_on'    : [],
            'spkgs'         : [],
            'patches'       : [],
            'authors'       : [],
            'participants'  : [],
        }

    rss = get_url("%s/ticket/%s?format=rss" % (TRAC_URL, ticket_id))
    tsv = parse_tsv(get_url("%s/ticket/%s?format=tab" % (TRAC_URL, ticket_id)))
    page_hash = digest(rss) # rss isn't as brittle
    if db is not None:
        # TODO: perhaps the db caching should be extracted outside of this function...
        db_info = db.lookup_ticket(ticket_id)
        if not force and db_info is not None and db_info['page_hash'] == page_hash:
            return db_info
    authors = set()
    patches = []
    for patch, who in extract_patches(rss):
        authors.add(who)
        patches.append(patch + "#" + digest(get_patch(ticket_id, patch)))
    authors = list(authors)
    data = {
        'id'            : ticket_id,
        'title'         : tsv['summary'],
        'page_hash'     : page_hash,
        'status'        : tsv['status'],
        'milestone'     : tsv['milestone'],
        'merged'        : tsv['merged'],
        'priority'      : tsv['priority'],
        'component'     : tsv['component'],
        'depends_on'    : extract_depends_on(tsv),
        'spkgs'         : extract_spkgs(tsv),
        'patches'       : patches,
        'authors'       : authors,
        'participants'  : extract_participants(rss),
        'git_branch'    : extract_git_branch(rss),
        'last_activity' : now_str(),
    }
    if db is not None:
        db.save_ticket(data)
        db_info = db.lookup_ticket(ticket_id)
        return db_info
    else:
        return data

def parse_tsv(tsv):
    header, data = tsv.split('\n', 1)
    def sanitize(items):
        for item in items:
            item = item.strip().replace('""', '"')
            if item and item[0] == '"' and item[-1] == '"':
                item = item[1:-1]
            yield item
    return dict(zip(sanitize(header.split('\t')),
                    sanitize(data.split('\t'))))

def extract_tag(sgml, tag):
    """
    Find the first occurance of the tag start (including attributes) and
    return the contents of that tag (really, up until the next end tag
    of that type).
    
    Crude but fast.
    """
    tag_name = tag[1:-1]
    if ' ' in tag_name:
        tag_name = tag_name[:tag_name.index(' ')]
    end = "</%s>" % tag_name
    start_ix = sgml.find(tag)
    if start_ix == -1:
        return None
    end_ix = sgml.find(end, start_ix)
    if end_ix == -1:
        return None
    return sgml[start_ix + len(tag) : end_ix].strip()

folded_regex = re.compile('all.*(folded|combined|merged)')
subsequent_regex = re.compile('second|third|fourth|next|on top|after')
attachment_regex = re.compile(r"<strong>attachment</strong>\s*set to <em>(.*)</em>", re.M)
rebased_regex = re.compile(r"([-.]?rebased?)|(-v\d)")
def extract_patches(rss):
    """
    Extracts the list of patches for a ticket from the rss feed.
    
    Tries to deduce the subset of attached patches to apply based on
    
        (1) "Apply ..." in comment text
        (2) Mercurial .N naming
        (3) "rebased" in name
        (3) Chronology
    """
    all_patches = []
    patches = []
    authors = {}
    for item in rss.split('<item>'):
        who = extract_tag(item, '<dc:creator>')
        description = extract_tag(item, '<description>').replace('&lt;', '<').replace('&gt;', '>')
        m = attachment_regex.search(description)
        comments = description[description.find('</ul>') + 1:]
        # Look for apply... followed by patch names
        for line in comments.split('\n'):
            if 'apply' in line.lower():
                new_patches = []
                for p in line[line.lower().index('apply') + 5:].split(','):
                    for pp in p.strip().split():
                        if pp in all_patches:
                            new_patches.append(pp)
                if new_patches or (m and not subsequent_regex.search(line)):
                    patches = new_patches
            elif m and folded_regex.search(line):
                patches = [] # will add this patch below
        if m is not None:
            attachment = m.group(1)
            base, ext = os.path.splitext(attachment)
            if '.' in base:
                try:
                    base2, ext2 = os.path.splitext(base)
                    count = int(ext2[1:])
                    for i in range(count):
                        if i:
                            older = "%s.%s%s" % (base2, i, ext)
                        else:
                            older = "%s%s" % (base2, ext)
                        if older in patches:
                            patches.remove(older)
                except:
                    pass
            if rebased_regex.search(attachment):
                older = rebased_regex.sub('', attachment)
                if older in patches:
                    patches.remove(older)
            if ext in ('.patch', '.diff'):
                all_patches.append(attachment)
                patches.append(attachment)
                authors[attachment] = who
    return [(p, authors[p]) for p in patches]

participant_regex = re.compile("<strong>attachment</strong>\w*set to <em>(.*)</em>")
def extract_participants(rss):
    """
    Extracts any spkgs for a ticket from the html page.
    """
    all = set()
    for item in rss.split('<item>'):
        who = extract_tag(item, '<dc:creator>')
        if who:
            all.add(who)
    return list(all)

git_branch_regex = re.compile(r"https://github.com/(\w+)/(\w+)/(?:tree/(\w+))?|(\b((git|http)://|\w+@)\S+\.git/? [a-zA-Z0-9_./-]+\b)")
def extract_git_branch(rss):
    commit = None
    for m in git_branch_regex.finditer(rss):
        if m.group(4):
            commit = m.group(4)
        else:
            commit = "git://github.com/%s/%s.git %s" % (m.group(1), m.group(2), m.group(3) or "master")
    return commit

spkg_url_regex = re.compile(r"(?:(?:https?://)|(?:/attachment/)).*?\.spkg")
#spkg_url_regex = re.compile(r"http://.*?\.spkg")
def extract_spkgs(tsv):
    """
    Extracts any spkgs for a ticket from the html page.
    
    Just searches for urls ending in .spkg.
    """
    return list(set(spkg_url_regex.findall(tsv['description'])))

def min_non_neg(*rest):
    non_neg = [a for a in rest if a >= 0]
    if len(non_neg) == 0:
        return rest[0]
    elif len(non_neg) == 1:
        return non_neg[0]
    else:
        return min(*non_neg)

ticket_url_regex = re.compile(r"%s/ticket/(\d+)" % TRAC_URL)
def extract_depends_on(tsv):
    deps_field = tsv['dependencies']
    deps = []
    for dep in re.finditer(r'#(\d+)', deps_field):
        deps.append(int(dep.group(1)))
    version = re.search(r'sage-\d+(\.\d)+(\.\w+)?', deps_field)
    if version:
        deps.insert(0, version.group(0))
    return deps



safe = re.compile('[-+A-Za-z0-9._]*')
def ensure_safe(items):
    """
    Raise an error if item has any spaces in it.
    """
    if isinstance(items, (str, unicode)):
        m = safe.match(items)
        if m is None or m.end() != len(items):
            raise ValueError, "Unsafe patch name '%s'" % items
    else:
        for item in items:
            ensure_safe(item)

def inplace_safe(branch):
    """
    Returns whether it is safe to test this ticket inplace.
    """
    # TODO: Are removed files sufficiently cleaned up?
    for file in subprocess.check_output(["git", "diff", "--name-only", "base..ticket_pristine"]).split('\n'):
        if not file:
            continue
        if file.startswith("src/sage") or file in ("src/setup.py", "src/module_list.py", "README.txt"):
            continue
        else:
            print "Unsafe file:", file
            return False
    return True

def pull_from_trac(sage_root, ticket, branch=None, force=None, interactive=None, inplace=None):
    if is_git(sage_root):
        ticket_id = ticket
        info = scrape(ticket_id)
        os.chdir(sage_root)
        do_or_die("git checkout base")
        if ticket_id == 0:
            return
        repo, branch = info['git_branch'].split(' ')
        do_or_die("git fetch %s +%s:ticket_pristine" % (repo, branch))
        do_or_die("git rev-list --left-right --count base..ticket_pristine")
        do_or_die("git log base..ticket_pristine")
        if inplace is None:
            inplace = inplace_safe("ticket")
        if not inplace:
            tmp_dir = tempfile.mkdtemp("-sage-git-temp-%s" % ticket_id)
            do_or_die("git clone . %s" % tmp_dir)
            os.chdir(tmp_dir)
            os.symlink(os.path.join(sage_root, "upstream"), "upstream")
            os.environ['SAGE_ROOT'] = tmp_dir
        try:
            do_or_die("git branch -D ticket")
        except Exception:
            pass
        do_or_die("git checkout -b ticket ticket_pristine")
        do_or_die("git merge -X patience base")
    else:
        # Should we set/unset SAGE_ROOT and SAGE_BRANCH here? Fork first?
        if branch is None:
            branch = str(ticket)
        if not os.path.exists('%s/devel/sage-%s' % (sage_root, branch)):
            do_or_die('%s/sage -b main' % (sage_root,))
            do_or_die('%s/sage -clone %s' % (sage_root, branch))
        os.chdir('%s/devel/sage-%s' % (sage_root, branch))
        if interactive:
            raise NotImplementedError
        if not os.path.exists('.hg/patches'):
            do_or_die('hg qinit')
            series = []
        elif not os.path.exists('.hg/patches/series'):
            series = []
        else:
            series = open('.hg/patches/series').read().split('\n')

        base = get_base(sage_root)
        desired_series = []
        seen_deps = []
        def append_patch_list(ticket, dependency=False):
            if ticket in seen_deps:
                return
            print "Looking at #%s" % ticket
            seen_deps.append(ticket)
            data = scrape(ticket)
            if dependency and 'closed' in data['status']:
                merged = data.get('merged')
                if merged is None:
                    merged = data.get('milestone')
                if merged is None or compare_version(merged, base) <= 0:
                    print "#%s already applied (%s <= %s)" % (ticket, merged, base)
                    return
            if data['spkgs']:
                raise NotImplementedError, "Spkgs not yet handled."
            if data['depends_on']:
                for dep in data['depends_on']:
                    if isinstance(dep, basestring) and '.' in dep:
                        if compare_version(base, dep) < 0:
                            raise ValueError, "%s < %s for %s" % (base, dep, ticket)
                        continue
                    append_patch_list(dep, dependency=True)
            print "Patches for #%s:" % ticket
            print "    " + "\n    ".join(data['patches'])
            for patch in data['patches']:
                patchfile, hash = patch.split('#')
                desired_series.append((hash, patchfile, get_patch_url(ticket, patchfile)))
        append_patch_list(ticket)
        
        ensure_safe(series)
        ensure_safe(patch for hash, patch, url in desired_series)

        last_good_patch = '-a'
        to_push = list(desired_series)
        for series_patch, (hash, patch, url) in zip(series, desired_series):
            if not series_patch:
                break
            next_hash = digest(open('.hg/patches/%s' % series_patch).read())
    #        print next_hash, hash, series_patch
            if next_hash == hash:
                to_push.pop(0)
                last_good_patch = series_patch
            else:
                break

        try:
            if last_good_patch != '-a':
                # In case it's not yet pushed...
                if last_good_patch not in os.popen2('hg qapplied')[1].read().split('\n'):
                    do_or_die('hg qpush %s' % last_good_patch)
            do_or_die('hg qpop %s' % last_good_patch)
            for hash, patch, url in to_push:
                if patch in series:
                    if not force:
                        raise Exception, "Duplicate patch: %s" % patch
                    old_patch = patch
                    while old_patch in series:
                        old_patch += '-old'
                    do_or_die('hg qrename %s %s' % (patch, old_patch))
                try:
                    do_or_die('hg qimport %s' % url)
                except Exception, exn:
                    time.sleep(30)
                    try:
                        do_or_die('hg qimport %s' % url)
                    except Exception, exn:
                        raise urllib2.HTTPError(exn)
                do_or_die('hg qpush')
            do_or_die('hg qapplied')
        except:
            os.system('hg qpop -a')
            raise


def push_from_trac(sage_root, ticket, branch=None, force=None, interactive=None):
    raise NotImplementedError



if __name__ == '__main__':
    force = False
    apply = False
    for ticket in sys.argv[1:]:
        if ticket == '-f':
            force = True
            continue
        if ticket == '-a':
            apply = True
            continue
        if '-' in ticket:
            start, end = ticket.split('-')
            tickets = range(int(start), int(end) + 1)
        else:
            tickets = [int(ticket)]
        for ticket in tickets:
            try:
                print ticket
                pprint.pprint(scrape(ticket, force=force))
                if apply:
                    pull_from_trac(os.environ['SAGE_ROOT'], ticket, force=True)
                time.sleep(1)
            except Exception:
                print "Error for", ticket
                traceback.print_exc()
        force = apply = False
#    pull_from_trac('/Users/robertwb/sage/current', ticket, force=True)
