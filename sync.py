#!/usr/bin/env python3

import sys
import os
import appdirs
import pathlib
import configparser
import random
import string
import time
import datetime
import dateutil
import pytz
import copy
import scripthelper
from os.path import exists
from contacts import Contacts

logger = scripthelper.getLogger(__name__)

all_sync_tags = set([])


def new_tag():
    """Return a new unique sync tag"""
    le = string.ascii_lowercase
    t = ''.join(random.choices(le, k=20))
    while t in all_sync_tags:
        t = ''.join(random.choices(le, k=20))
    all_sync_tags.add(t)
    return t


def duplicates(ls: list):
    """Return a set of duplicates in a list."""

    seen = set([])
    dups = set([])

    for x in ls:
        if x in seen:
            dups.add(x)
        else:
            seen.add(x)

    return dups


def load_config(cfile):
    """Return the config, or make a default one.

    Parameters
    ----------
    cfile: pathlib.Path
        Path to the config file

    Returns
    -------
    configparser.ConfigParser:
        The configuration, but only if it already exists and isn't the default,
        otherwise just exit

    """
    logger.verbose(f"loaded {cfile}")
    # put in default config file if necessary
    if not cfile.exists():
        cp = configparser.ConfigParser()

        cp['DEFAULT'] = {
            'msg': 'You need an account section for each user, please setup',
            'last': '1972-01-01:T00:00:00+00.00'
        }
        cp['account-FIXME'] = {
            'user': 'FIXME@gmail.com',
            'keyfile': f'{cfile.parent}/FIXME_keyfile.json',
            'credfile': f'{cfile.parent}/FIXME_token'
        }
        with open(cfile, 'w') as cfh:
            cp.write(cfh)

        logger.warning(f"Made config file {cfile}, you must edit it")
        sys.exit(1)

    cp = configparser.ConfigParser()
    cp.read(cfile)
    if 'account-FIXME' in cp.sections():
        logger.critical(f"You must edit {cfile}.  There is an account-FIXME section")
        sys.exit(2)

    return cp


def save_config(cp, cfile):
    """Update the last run, and save"""
    cp['DEFAULT'] = {
        # +1s because it happens that the server time of the last updated
        # element is greater than the one saved on the config.ini (do not ask
        # me why )
        'last': (
            datetime.datetime.utcnow() + datetime.timedelta(seconds=1)
        ).replace(tzinfo=pytz.utc).isoformat()
    }
    with open(cfile, 'w') as cfh:
        cp.write(cfh)


def remove_prefix(text, prefix):
    if text.startswith(prefix):
        return text[len(prefix):]
    return text  # or whatever


scripthelper.parser.description = """
Sync google contacts.

If you have previously used github.com/michael-adler/sync-google-contacts which
uses the csync-uid, after enabling the People API on all your accounts,
editting your config file (you will be prompted about that), you should be all
good to go.

If you haven't synced contacts before you will have to go through an --init
phase, again you will be prompted.

For full instructions see
https://github.com/mrmattwilkins/google-contacts-sync
"""
scripthelper.add_argument(
    '--init', action='store_true',
    help="Initialize by syncing using names"
)
scripthelper.add_argument(
    '--rlim', type=int,
    help="If --init, wait this many seconds between each sync"
)
args = scripthelper.initialize()

# get the configuration file
logger.verbose('Loading configuration')
if exists("PORTABLE.md"):
    cdir = pathlib.Path(
        "conf"
    )
else:
    cdir = pathlib.Path(
        appdirs.AppDirs('google-contacts-sync', 'mcw').user_data_dir
    )

os.makedirs(cdir, mode=0o755, exist_ok=True)
cfile = cdir / 'config.ini'
cp = load_config(cfile)

# get the contacts for each user
logger.verbose('Getting contacts')
con = {
    cp[s]['user']: Contacts(
        cp[s]['keyfile'], cp[s]['credfile'], cp[s]['user']
    )
    for s in cp.sections()
}

if args.init:
    logger.info("Setting up syncing using names to identify identical contacts")

    # get all the names to see if there are duplicates
    for email, acc in con.items():
        dups = duplicates([i['name'] for i in acc.info.values()])
        if dups:
            logger.info(
                f"These contacts ({','.join(dups)}) are duplicated in account "
                f"{email}. I will not continue, this will cause confusion"
            )
            logger.error("Please remove your duplicates and try again")
            sys.exit(1)

    # keep track of who we have synced so we don't redo them on next account
    done = set([])
    for email, acc in con.items():
        # number seen before by this account, and number pushed
        ndone = 0
        nsync = 0
        for rn, p in acc.info.items():
            if p['name'] in done:
                ndone += 1
            else:
                if p['tag'] is None:
                    p['tag'] = new_tag()
                    acc.update_tag(rn, p['tag'])
                newcontact = acc.get(rn)
                for otheremail, otheracc in con.items():
                    if otheracc == acc:
                        continue
                    rn = otheracc.name_to_rn(p['name'])
                    if rn:
                        otheracc.update_tag(rn, p['tag'])
                        otheracc.update(p['tag'], newcontact)
                    else:
                        otheracc.add(newcontact)
                done.add(p['name'])
                nsync += 1
                # back-off a bit so google doesn't rate limit us
                if args.rlim and args.rlim > 0:
                    time.sleep(args.rlim)

            logger.info(
                f"Pushing {email} (tot {len(acc.info)}): "
                f"synced {nsync}, done before {ndone}"
            )

    # update the last updated field
    save_config(cp, cfile)
    sys.exit(0)

# if an account has no sync tags, the user needs to do a --init
logger.verbose('Checking no new accounts')
for email, acc in con.items():
    if all([v['tag'] is None for v in acc.info.values()]):
        logger.critical(
            f'{email} has no sync tags.  It looks like this is the first time '
            'running this script for this account.  You need to pass --init '
            'for me to assign the sync tag to each contact'
        )
        sys.exit(2)

# ======================================
# Sync ContactGroup
# ======================================
logger.verbose("ContactGroups synchronization...")
all_sync_tags_ContactGroups = set([])
for email, acc in con.items():
    all_sync_tags_ContactGroups.update([
        v['tag'] for v in acc.info_group.values() if v['tag'] is not None
    ])


# deletions are detected by missing tags, store the tags to delete in here
logger.verbose('ContactGroups - Checking what to delete')
todel = set([])
for email, acc in con.items():
    # tags in acc
    tags = set(
        v['tag'] for v in acc.info_group.values()
        if v['tag'] is not None
    )
    rm = all_sync_tags_ContactGroups - tags
    if rm:
        logger.info(f'{email}: {len(rm)} ContactGroup(s) deleted')
    todel.update(rm)
if todel:
    for email, acc in con.items():
        logger.info(f'removing ContactGroups from {email}: ')
        for tag in todel:
            acc.delete_contactGroup(tag)


# if there was anything deleted, get all contact info again (so those removed
# are gone from our cached lists)
if todel:
    for acc in con.values():
        acc.get_info()


# new group won't have a tag
logger.verbose('ContactGroups - Checking for new ContactGroup')
added = []
for email, acc in con.items():
    # maps tag to (rn, name)
    toadd = [
        (rn, v['name'])
        for rn, v in acc.info_group.items() if v['tag'] is None
    ]
    if toadd:
        logger.verbose(f'{email}: these are new {list(i[1] for i in toadd)}')
    for rn, name in toadd:

        # assign a new tag to this ContactGroup
        tag = new_tag()
        acc.update_contactGroup_tag(rn, tag)
        newcontact = acc.get_contactGroup(rn)

        # record this is a new ContactGroup so we won't try syncing them laster
        added.append((acc, rn))

        # now add them to all the other accounts
        for otheremail, other in con.items():
            if other == acc:
                continue
            logger.verbose(f'adding {name} to {otheremail}')

            tmp = {
                "contactGroup": {
                    "name": newcontact["name"],
                    "clientData": newcontact["clientData"]
                }
            }
            p = other.add_contactGroup(tmp)
            added.append((other, p['resourceName']))

# updates.  we want to see who has been modified since last run.  of course
# anyone just added will have been modified, so ignore those in added
lastupdate = dateutil.parser.isoparse(cp['DEFAULT']['last'])

# maps tag to [(acc, rn, updated)] where update must be newer than our last run
t2aru = {}

for email, acc in con.items():
    tru = [
        (v['tag'], rn, v['updated'])
        for rn, v in acc.info_group.items()
        if v['updated'] > lastupdate and (acc, rn) not in added
    ]
    for t, rn, u in tru:
        t2aru.setdefault(t, []).append((acc, rn, u))

logger.verbose(f"ContactGroups - There are {len(t2aru)} contactGroups to update")
for tag, val in t2aru.items():
    # find the account with most recent update
    newest = max(val, key=lambda x: x[2])
    acc, rn = newest[:2]
    logger.verbose(f"{acc.info_group[rn]['name']}: ")
    contactGroup = acc.get_contactGroup(rn)
    for otheremail, otheracc in con.items():
        if otheracc == acc:
            continue
        logger.verbose(f"- {otheremail} ")
        otheracc.update_contactGroup(tag, contactGroup)

# ======================================
# Sync Contact
# ======================================
logger.verbose("Contacts synchronization...")
# we need a full set of tags so we can detect changes.  ignore those that don't
# have a tag yet, they will be additions
for email, acc in con.items():
    all_sync_tags.update([
        v['tag'] for v in acc.info.values() if v['tag'] is not None
    ])

# deletions are detected by missing tags, store the tags to delete in here
logger.verbose('Checking what to delete')
todel = set([])
for email, acc in con.items():
    # tags in acc
    tags = set(v['tag'] for v in acc.info.values() if v['tag'] is not None)
    rm = all_sync_tags - tags
    if rm:
        logger.verbose(f'{email}: {len(rm)} contact(s) deleted')
    todel.update(rm)
if todel:
    for email, acc in con.items():
        logger.verbose(f'removing contacts from {email}: ')
        for tag in todel:
            acc.delete(tag)

# if there was anything deleted, get all contact info again (so those removed
# are gone from our cached lists)
if todel:
    for acc in con.values():
        acc.get_info()

# new people won't have a tag
logger.verbose('Checking for new people')
added = []
for email, acc in con.items():
    # maps tag to (rn, name)
    toadd = [
        (rn, v['name'])
        for rn, v in acc.info.items() if v['tag'] is None
    ]
    if toadd:
        logger.verbose(f'{email}: these are new {list(i[1] for i in toadd)}')
    for rn, name in toadd:

        # assign a new tag to this person
        tag = new_tag()
        acc.update_tag(rn, tag)
        newcontact = acc.get(rn)

        # record this is a new person so we won't try syncing them laster
        added.append((acc, rn))

        # ADD PERSON WITH LABEL ( ContactGroup )
        #
        # Before adding a new person, check which ContactGroup he is in
        # if it is only in the standard one (myContacts - it has no label)
        #   I continue as old code
        # if it is 1 or more -> get the label sync tag
        #   I look for the tag in the list of labels of the other account (I
        #   retrieve the ResourceName)
        # set the correct resource name
        # get RN of the contactGroup - labels ( except myContacts)
        groupRNs = [
            grp["contactGroupMembership"]["contactGroupResourceName"]
            for grp in newcontact["memberships"]
            if grp["contactGroupMembership"]["contactGroupId"] != "myContacts"
        ]
        # get syncTag for each RN
        groupTags = [
            acc.rn_to_tag_contactGroup(groupRN) for groupRN in groupRNs
        ]

        # remove all contactGroup ( label ) ( except myContacts)
        newcontact["memberships"] = [
            grp
            for grp in newcontact["memberships"]
            if grp["contactGroupMembership"]["contactGroupId"] == "myContacts"
        ]

        p = None

        # now add them to all the other accounts
        for otheremail, other in con.items():

            if other == acc:
                continue
            logger.verbose(f'adding {name} to {otheremail}')

            # if there are tags to sync
            if len(groupTags) > 0:
                newcontactCopy = copy.deepcopy(newcontact)
                for groupTag in groupTags:
                    # retrieving the RN of other client based on the sync tag
                    groupRN_other = other.tag_to_rn_contactGroup(groupTag)
                    # add it to contact

                    groupID_other = remove_prefix(
                        groupRN_other, "contactGroups/"
                    )
                    newcontactCopy["memberships"].append({
                        'contactGroupMembership': {
                            'contactGroupId': groupID_other,
                            'contactGroupResourceName': groupRN_other
                        }
                    })

                p = other.add(newcontactCopy)

            else:   # if there aren't any, I just add
                p = other.add(newcontact)
            added.append((other, p['resourceName']))

# updates.  we want to see who has been modified since last run.  of course
# anyone just added will have been modified, so ignore those in added

lastupdate = dateutil.parser.isoparse(cp['DEFAULT']['last'])

# maps tag to [(acc, rn, updated)] where update must be newer than our last run
t2aru = {}

for email, acc in con.items():
    tru = [
        (v['tag'], rn, v['updated'])
        for rn, v in acc.info.items()
        if v['updated'] > lastupdate and (acc, rn) not in added
    ]
    for t, rn, u in tru:
        t2aru.setdefault(t, []).append((acc, rn, u))

logger.verbose(f"There are {len(t2aru)} contacts to update")
for tag, val in t2aru.items():
    # find the account with most recent update
    newest = max(val, key=lambda x: x[2])
    acc, rn = newest[:2]
    logger.verbose(f"{acc.info[rn]['name']}: ")
    contact = acc.get(rn)

    # before sending the update
    # I take all the RNs of the labels  (except myContacts)
    # get the label sync tag
    # for each tag, get the RN in the other account

    # get RN of the contactGroup - labels (except myContacts)
    groupRNs = [
        grp["contactGroupMembership"]["contactGroupResourceName"]
        for grp in contact["memberships"]
        if grp["contactGroupMembership"]["contactGroupId"] != "myContacts"
    ]
    # get syncTag for each RN
    groupTags = [
        acc.rn_to_tag_contactGroup(groupRN) for groupRN in groupRNs
    ]

    # remove all contactGroup ( label ) ( except myContacts)
    contact["memberships"] = [
        grp
        for grp in contact["memberships"]
        if grp["contactGroupMembership"]["contactGroupId"] == "myContacts"
    ]

    for otheremail, otheracc in con.items():
        if otheracc == acc:
            continue
        logger.verbose(f"{otheremail} ")

        if len(groupTags) > 0:
            contactCopy = copy.deepcopy(contact)
            for tag in groupTags:
                # retrieving the RN of the other client based on the sync tag
                rn = otheracc.tag_to_rn_contactGroup(tag)
                # might be None if tag was starred or other system group
                if not rn:
                    continue
                gid = remove_prefix(rn, "contactGroups/")
                contactCopy["memberships"].append({
                    'contactGroupMembership': {
                        'contactGroupId': gid,
                        'contactGroupResourceName': rn
                    }
                })

            otheracc.update(tag, contactCopy)
        else:
            otheracc.update(tag, contact)

# update the last updated field
save_config(cp, cfile)
