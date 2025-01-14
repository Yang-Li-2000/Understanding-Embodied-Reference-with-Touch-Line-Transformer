import cv2
import os
import os.path as osp
import pickle

def draw_box(img,box,img_name):
    pt1 = (int(box[0]),int(box[1]))
    pt2 = (int(box[2]),int(box[3]))
    cv2.rectangle(img,pt1,pt2,(0,255,0),2)
    cv2.imwrite(img_name,img)





for img_name in os.listdir('./yourefit/images'):
    pickle_file = osp.join('./yourefit/pickle',img_name[:-4]+'.p')
    pick = pickle.load(open(pickle_file, "rb" ))
    img = cv2.imread('./images/'+img_name)
    bbox = pick['bbox']
    draw_box(img,bbox,'./gt_vis/'+img_name)