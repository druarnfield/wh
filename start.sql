# serve.sql
FORCE INSTALL quack FROM core_nightly;
LOAD quack;
CALL quack_serve('quack:localhost:9494',
    allow_other_hostname => true,
    token = 'E37978A0131391E93D313831C45A0358');
