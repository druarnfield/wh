FORCE INSTALL quack FROM core_nightly;
LOAD quack;
ATTACH 'quack:localhost:9494' AS metrics (
                 TOKEN 'E37978A0131391E93D313831C45A0358',
                 DISABLE_SSL true);
