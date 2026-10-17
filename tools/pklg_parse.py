import struct, collections, sys, pickle

if len(sys.argv) < 2:
    sys.exit('usage: pklg_parse.py <capture.pklg> [out.pkl]')
d=open(sys.argv[1],'rb').read()
recs=[]; off=0
while off+4<=len(d):
    L=struct.unpack('<I',d[off:off+4])[0]
    if L<9 or off+4+L>len(d): break
    body=d[off+4:off+4+L]
    ts=struct.unpack('<II',body[:8]); typ=body[8]; payload=body[9:]
    recs.append((ts,typ,payload)); off+=4+L
print("records",len(recs), "leftover", len(d)-off)
print(collections.Counter(t for _,t,_ in recs))

ATT={0x01:'ErrRsp',0x02:'MTUReq',0x03:'MTURsp',0x04:'FindInfoReq',0x05:'FindInfoRsp',0x06:'FindByTypeReq',0x07:'FindByTypeRsp',
0x08:'ReadByTypeReq',0x09:'ReadByTypeRsp',0x0A:'ReadReq',0x0B:'ReadRsp',0x0C:'ReadBlobReq',0x0D:'ReadBlobRsp',0x10:'ReadByGroupReq',
0x11:'ReadByGroupRsp',0x12:'WriteReq',0x13:'WriteRsp',0x16:'PrepWriteReq',0x17:'PrepWriteRsp',0x18:'ExecWriteReq',0x19:'ExecWriteRsp',
0x1B:'Notify',0x1D:'Indicate',0x1E:'IndConfirm',0x52:'WriteCmd'}
frames=[]
for i,(ts,typ,p) in enumerate(recs):
    if typ not in (2,3) or len(p)<4: continue
    hlen=struct.unpack('<H',p[2:4])[0]
    acl=p[4:4+hlen]
    if len(acl)<4: continue
    l2len,cid=struct.unpack('<HH',acl[:4])
    if cid!=4: continue
    frames.append((i,ts,typ,acl[4:4+l2len]))
print("ATT frames",len(frames))
c=collections.Counter((typ,ATT.get(a[0],hex(a[0]))) for _,_,typ,a in frames if a)
for k,v in c.most_common(): print(k,v)
pickle.dump(frames,open(sys.argv[2] if len(sys.argv)>2 else 'att.pkl','wb'))
