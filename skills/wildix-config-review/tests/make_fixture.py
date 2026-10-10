#!/usr/bin/env python3
"""Build a small synthetic WMS backup for tests. No real customer data.

Usage: python make_fixture.py <output.tar.gz>
The secrets below are fake markers; tests assert they never reach parser output.
"""
import io
import struct
import sys
import tarfile

FAKE_SECRETS = ['FIXTURE-SMTP-SECRET', 'FIXTURE-TRUNK-SECRET', 'fixtureauthuser', 'FIXTURE-LDAP-SECRET',
                'FIXTURE-REG-SECRET', 'FIXTURE-PIN-9999']

WMSDB_SQL = r"""
CREATE TABLE `system` (`variable` varchar(20) PRIMARY KEY, `value` varchar(50));
INSERT INTO system VALUES('version','1.1');
INSERT INTO system VALUES('SMTP','{"host":"smtp.example.test","user":"pbx","password":"FIXTURE-SMTP-SECRET","port":587}');
CREATE TABLE `dialplan_rulenames` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `rulename` varchar(127) UNIQUE,
  `ruledescription` varchar(127), `subrule_id` int(11));
INSERT INTO dialplan_rulenames VALUES(1,'users','Internal dialplan','');
INSERT INTO dialplan_rulenames VALUES(2,'inbound','From carrier',NULL);
INSERT INTO dialplan_rulenames VALUES(3,'park return','',NULL);
INSERT INTO dialplan_rulenames VALUES(4,'unused','',NULL);
INSERT INTO dialplan_rulenames VALUES(5,'empty','',NULL);
INSERT INTO dialplan_rulenames VALUES(6,'receptionist','',NULL);
CREATE TABLE `dialplan_rules` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `rule_id` int(11), `number` varchar(15),
  `exmatch` int(1), `priority` int(3), `application` varchar(40), `parameters` varchar(255),
  `dialplanappname` varchar(40), `comment` varchar(255) NOT NULL default '');
INSERT INTO dialplan_rules VALUES(10,1,'NXXXXXXXXX',NULL,0,'DialTrunk','"+1${EXTEN}","Example%20Trunk","","","","3"','app-dial-trunk','local');
INSERT INTO dialplan_rules VALUES(11,1,'901',NULL,0,'Goto','"9001","park return","1",""','app-goto-general','Call back from Park');
INSERT INTO dialplan_rules VALUES(20,2,'+15550100',NULL,0,'Set','"__CALLTYPE","external"','app-set','');
INSERT INTO dialplan_rules VALUES(21,2,'+15550100',NULL,1,'DialPhone','"201","20","",""','app-dial-internalcall','');
INSERT INTO dialplan_rules VALUES(22,2,'+15550100',NULL,2,'Queue','"1","t","","","30",""','app-queue','');
INSERT INTO dialplan_rules VALUES(23,2,'+15550100',NULL,3,'VoiceMail','"201",""','app-voicemail','');
INSERT INTO dialplan_rules VALUES(24,2,'+1555XXXXXXX',NULL,0,'Goto','"+15550100","inbound","1",""','app-goto-general','catch-all');
INSERT INTO dialplan_rules VALUES(50,6,'s',NULL,0,'Goto','"9001","park return","1",""','app-goto-general','');
INSERT INTO dialplan_rules VALUES(40,2,'+15550200',NULL,0,'GotoIfCallType','"blacklist","9001","park return","1",""','app-goto-if-call-type','');
INSERT INTO dialplan_rules VALUES(41,2,'+15550200',NULL,1,'Answer','','Answer','');
INSERT INTO dialplan_rules VALUES(42,2,'+15550200',NULL,2,'GotoIfSwitchIsOn','"1","202","pbxinternal","1",""','app-goto-if-switch-is-on','switch gates');
INSERT INTO dialplan_rules VALUES(43,2,'+15550200',NULL,3,'GotoIf3SwitchIsOnOrBlinking','"2","201","pbxinternal","1","","+15550100","inbound","1",""','app-goto-if-three-switch-is-on-or-blinking','switch gates');
INSERT INTO dialplan_rules VALUES(44,2,'+15550200',NULL,4,'GotoIfBusyCongested','"BUSY","202","pbxinternal","1",""','app-goto-if-dialstatus','switch gates');
INSERT INTO dialplan_rules VALUES(45,2,'+15550200',NULL,5,'VoiceMail','"202",""','app-voicemail','switch gates');
INSERT INTO dialplan_rules VALUES(30,3,'9001',NULL,0,'Set','"CALLERID(name)","PARKED ${name}"','app-set','Call back from Park');
INSERT INTO dialplan_rules VALUES(31,3,'9001',NULL,1,'Queue','"1","t","","","15",""','app-queue','Call back from Park');
INSERT INTO dialplan_rules VALUES(32,3,'9001',NULL,2,'Goto','"9001","park return","1",""','app-goto-general','Call back from Park');
CREATE TABLE `dialplan_rules_includes` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `rule_id` int(11), `priority` int(3),
  `include_rule_id` int(11));
INSERT INTO dialplan_rules_includes VALUES(1,1,0,-4);
CREATE TABLE `dialplan_links`(id INTEGER PRIMARY KEY AUTOINCREMENT, object_from_type INTEGER, object_from_id INTEGER,
  object_to_type INTEGER, object_to_id VARCHAR(80));
CREATE TABLE `call_groups` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `call_groupname` varchar(40) UNIQUE,
  `queue_parameters` text);
INSERT INTO call_groups VALUES(1,'Reception','{"timeout":15,"strategy":"ringall","retry":5,"wrapuptime":0,"maxlen":0,"joinempty":"invalid","queue-youarenext":"queue-youarenext"}');
INSERT INTO call_groups VALUES(2,'Empty','{"timeout":20,"strategy":"linear"}');
CREATE TABLE `call_groups_members` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `call_group_id` int(11),
  `group_member` varchar(20), `group_membername` varchar(40), `penalty` int(11));
INSERT INTO call_groups_members VALUES(1,1,'201@internalcalls','Local/201@internalcalls',NULL);
INSERT INTO call_groups_members VALUES(2,1,'202@internalcalls','Local/202@internalcalls',NULL);
CREATE TABLE `trunks` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `username` varchar(20), `title` varchar(40) NOT NULL default '',
  `register` varchar(255), `settings_pbx` text, `settings_wms` text);
INSERT INTO trunks VALUES(1,'fixtureauthuser','Example Trunk','Example%20Trunk?fixtureauthuser:FIXTURE-REG-SECRET:"fixtureauthuser"@sip.example.test~120','{"context":"inbound","host":"sip.example.test","secret":"FIXTURE-TRUNK-SECRET","username":"fixtureauthuser","allow":"ulaw","dtmfmode":"rfc2833"}','{"enable_reg":"1"}');
CREATE TABLE `pstn_trunks` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `username` varchar(20) UNIQUE, `technology` varchar(5),
  `ports` int(4), `settings_pbx` text, `settings_mgw` text);
CREATE TABLE `outgoing_groups_main` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `groupname` varchar(63) UNIQUE,
  `description` varchar(255) NOT NULL default '');
CREATE TABLE `outgoing_groups_rules` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `group_id` int(11), `priority` int(11),
  `trunkname` varchar(255), `prepend` varchar(15), `remove` varchar(15), `max_calls` int(11), `timeout` int(11),
  `rules_cnt` int(11));
CREATE TABLE `timetable_main` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `timetable` varchar(40) UNIQUE, `desc` text,
  `state` int(1));
CREATE TABLE `timetable_additional` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `parent_id` int(6), `day_of_week_from` int(1),
  `day_of_week_to` int(1), `day_id_from` int(2), `day_id_to` int(2), `month_id` int(2), `start_daytime` time,
  `end_daytime` time, `year_id` int(4), `enabled_id` tinyint(1));
CREATE TABLE `wmsswitches` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `switchname` varchar(40), `description` text,
  `state` int(1), `swtype` int(1), `swnametype` varchar(41) UNIQUE);
INSERT INTO wmsswitches VALUES(1,'Night Mode','',1,0,NULL);
INSERT INTO wmsswitches VALUES(2,'Office Mode','',2,3,NULL);
CREATE TABLE `ivr_tree` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `parent_id` int(11), `command` varchar(200),
  `parameters` text, `number` varchar(20), `treeid` int(11));
CREATE TABLE `paging` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `name` varchar(40) UNIQUE);
CREATE TABLE `paging_members` (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `paging_id` int(11), `group_member` varchar(20));
"""


def ldif_user(uid, cn, ext, kind, dialplan):
    return (f"dn: uid={uid},o=com0,dc=wildix\nobjectClass: wPerson\nuid: {uid}\ncn: {cn}\n"
            f"telephoneNumber: {ext}\nuserKind: {kind}\nuserDialplan: {dialplan}\n"
            f"owner: gid=1,o=com0,dc=wildix\n"
            f"userPassword:: {__import__('base64').b64encode(b'FIXTURE-LDAP-SECRET').decode()}\n"
            f"userPIN: FIXTURE-PIN-9999\n\n")


LDIF = ("dn: dc=wildix\no: wildix\n\n"
        "dn: gid=1,o=com0,dc=wildix\ngid: 1\ncn: Default\nwcgrp: 1\n\n"
        + ldif_user(1001, 'Front Desk', 201, 'user', 'users')
        + ldif_user(1002, 'Back Office With A Name Long Enough To Be Fol', 202, 'user', 'users').replace(
            'cn: Back Office With A Name Long Enough To Be Fol', 'cn: Back Office With A Name Long Enough To Be Fol\n ded')
        + ldif_user(1901, 'Park 1', 901, 'park_orbit', 'users')
        + ldif_user(1902, 'Park 2', 902, 'park_orbit', 'users'))

CONFIGS = {
    'timezone': 'America/Edmonton\n',
    'pbx/company.conf': 'pbx_domain="fixture.wildixin.com"\n',
    'callweaver/extensions-variables.conf': 'GPARK_TIMEOUT=45000\nGINTDIAL_TIMEOUT=60\nGCOUNTRY_CODE=1\n',
    'callweaver/features.conf': '[general]\nparkingtime => 45\n',
    'callweaver/queues-entities.conf': '[1]\nqueue-name=Reception\ntimeout=15\nmember => Local/201@internalcalls,1\n',
    'callweaver/sip-managed.conf': '[managed]\nhost=operator.example.test\nsecret=FIXTURE-TRUNK-SECRET\n'
                                   'username=fixtureauthuser\ncontext=empty\ntype=peer\nallow=alaw\nallow=ulaw\n',
}


# ---- tiny Lua 5.1 bytecode writer (table constructors only)

def _lua_string(s):
    b = s.encode() + b'\0'
    return struct.pack('<Q', len(b)) + b


def lua_chunk(globals_):
    consts, code = [], []

    def k(v):
        if v not in consts:
            consts.append(v)
        idx = consts.index(v)
        assert idx < 256
        return idx

    def abc(op, a, b, c):
        code.append(op | a << 6 | c << 14 | b << 23)

    def abx(op, a, bx):
        code.append(op | a << 6 | bx << 14)

    def build(tbl, reg):
        abc(10, reg, 0, 0)                       # NEWTABLE
        for key, val in tbl.items():
            if isinstance(val, dict):
                build(val, reg + 1)
                abc(9, reg, 0x100 | k(key), reg + 1)   # SETTABLE R[reg][K] = R[reg+1]
            else:
                abc(9, reg, 0x100 | k(key), 0x100 | k(val))

    for name, tbl in globals_.items():
        build(tbl, 0)
        abx(7, 0, k(name))                       # SETGLOBAL
    abc(30, 0, 1, 0)                             # RETURN

    out = b'\x1bLua' + bytes([0x51, 0, 1, 4, 8, 4, 8, 0])
    out += _lua_string('fixture.lua') + struct.pack('<ii', 0, 0) + bytes([0, 0, 2, 250])
    out += struct.pack('<i', len(code)) + b''.join(struct.pack('<I', i) for i in code)
    out += struct.pack('<i', len(consts))
    for c in consts:
        out += b'\x04' + _lua_string(c)
    out += struct.pack('<iiii', 0, 0, 0, 0)       # protos, lineinfo, locvars, upvalues
    return out


def _feat(**on):
    base = {c: {'status': '0', 'value': ''} for c in ('FCF', 'BCF', 'UCF', 'TOUT', 'MEXT')}
    for code, val in on.items():
        base[code] = {'status': '1', 'value': val}
    return base


USERS_LUA = {
    '1001.lua': {'features': {
        'STATUS': {'value': ''},
        '_classes': {'external': {'default': _feat(UCF='VOICEMAIL', TOUT='25')},
                     'internal': {'default': _feat()}},
        'FKEYS': 'LINE  ,PORBIT 901 Park_1 1 0 1,PORBIT 999 Ghost 1 0 1',
        'TWOFACTOR': {'secret': 'FIXTURE-PIN-9999'},
    }},
    '1002.lua': {'features': {
        'STATUS': {'value': ''},
        '_classes': {'external': {'default': _feat()}, 'internal': {'default': _feat()}},
        'FKEYS': 'LINE  ,EXT 201  1 0 1',
    }},
}


def tar_bytes(files, gz=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz' if gz else 'w') as t:
        for name, data in files.items():
            data = data.encode() if isinstance(data, str) else data
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def build_autotrace(path, serial='abcdef012345', domain='fixture'):
    """A minimal autotrace archive whose logs identify the PBX."""
    wms = (f"Oct  2 09:00:00 {domain} wms: license serial [{serial}]: pbxid [{serial}]\n"
           f"Oct  2 09:00:01 {domain} nginx: GET https://{domain}.wildixin.com/api -0600\n"
           f"Oct  2 09:00:02 {domain} kamailio: sip:{serial}.wildixin.com\n")
    with open(path, 'wb') as f:
        f.write(tar_bytes({'mnt/backups/autotrace/wms.log': wms}, gz=True))
    return path


def build(path):
    users = {name: lua_chunk(g) for name, g in USERS_LUA.items()}
    outer = {
        'wmsdb.sql': WMSDB_SQL,
        'ldapcat.ldif': LDIF,
        'configs.tar': tar_bytes(CONFIGS),
        'users.tar': tar_bytes(users),
    }
    with open(path, 'wb') as f:
        f.write(tar_bytes(outer, gz=True))
    return path


if __name__ == '__main__':
    build(sys.argv[1] if len(sys.argv) > 1 else 'fixture_2026.01.02_03.04.05_7.08.1_00aa_wms_b_test.tar.gz')
