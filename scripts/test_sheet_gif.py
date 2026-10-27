"""Regression checks for low-alpha fringes in actual decoded v3 GIFs."""
import io
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image, ImageDraw, GifImagePlugin
import sheet_images

from sheet_images import assemble_sheet, _gif_bytes


def exact_gif_fixture(frames, *, loop=False, duration=150, disposal=2, transparency=255):
    """Explicit paletted descriptors: fixtures must keep identical adjacent frames."""
    info={'background':255,'duration':duration}
    if transparency is not None:info['transparency']=transparency
    if loop:info['loop']=0
    header,_=GifImagePlugin.getheader(frames[0].copy(),info=info)
    payload=b''.join(header)
    for i,frame in enumerate(frames):
        options={'duration':duration,'disposal':disposal[i] if isinstance(disposal,list) else disposal,'include_color_table':True}
        if transparency is not None:options['transparency']=transparency
        payload+=b''.join(GifImagePlugin.getdata(frame.copy(),**options))
    return payload+b';'


def exact_palette_frames(count=8, colors=180):
    frames=[]
    for i in range(count):
        frame=Image.new('P',(24,20),255)
        palette=[channel for j in range(255) for channel in ((j+i*29)%256,(j*3+i*31)%256,(j*7+i*37)%256)]+[0,0,0]
        frame.putpalette(palette)
        for j in range(colors):frame.putpixel((2+j%20,2+j//20),j)
        frames.append(frame)
    return frames


class ExactTranslatedGifTests(unittest.TestCase):
    def encode(self,payload,shifts,**kwargs):
        self.assertTrue(callable(getattr(sheet_images,'_translated_gif_bytes',None)),
                        'Alignment must translate the verified GIF, not requantize PNG artwork')
        transforms=[{'frame_index':i,'dx':dx,'dy':0,'scale':1} for i,dx in enumerate(shifts)]
        return sheet_images._translated_gif_bytes(payload,transforms,input_canvas=(24,20),
            output_canvas=(30,20),frame_ms=150,loop=kwargs.pop('loop',False),**kwargs)

    def test_exact_local_palettes_union_over_255_and_identical_adjacent_frames(self):
        frames=exact_palette_frames();frames[4]=frames[3].copy()
        for loop in (False,True):
            payload=exact_gif_fixture(frames,loop=loop);before=bytes(payload)
            shifts=[1,-1,2,0,0,3,1,2];result=self.encode(payload,shifts,loop=loop)
            self.assertEqual(result,self.encode(payload,shifts,loop=loop));self.assertEqual(payload,before)
            with Image.open(io.BytesIO(payload)) as parent,Image.open(io.BytesIO(result)) as child:
                self.assertEqual(child.n_frames,8);self.assertEqual(child.info.get('loop'),0 if loop else None)
                for i,dx in enumerate(shifts):
                    parent.seek(i);child.seek(i);expected=Image.new('RGBA',(30,20))
                    expected.paste(parent.convert('RGBA'),(dx,0));actual=child.convert('RGBA')
                    self.assertEqual(actual.getchannel('A').tobytes(),expected.getchannel('A').tobytes())
                    self.assertEqual(sheet_images._canonical_rgba_bytes(actual),sheet_images._canonical_rgba_bytes(expected))
                    self.assertEqual(child.info['duration'],150)

    def test_source_disposal_one_two_three_and_partial_frame_composition(self):
        frames=exact_palette_frames(colors=10)
        for i,frame in enumerate(frames):frame.putpixel((12+i,15),7)
        for disposal in (1,2,3,[1,2,3,1,2,3,1,2]):
            with self.subTest(disposal=disposal):
                payload=exact_gif_fixture(frames,disposal=disposal)
                result=self.encode(payload,[1]*8)
                with Image.open(io.BytesIO(payload)) as parent,Image.open(io.BytesIO(result)) as child:
                    for i in range(8):
                        parent.seek(i);child.seek(i);expected=Image.new('RGBA',(30,20));expected.paste(parent.convert('RGBA'),(1,0))
                        self.assertEqual(sheet_images._canonical_rgba_bytes(child.convert('RGBA')),sheet_images._canonical_rgba_bytes(expected))

    def test_256_opaque_colors_plus_transparent_margin_blocks_without_quantization(self):
        frames=exact_palette_frames(count=2,colors=255)
        # Disposal 1 composes the old 255 colors with a new local-table color.
        overlay=Image.new('P',(24,20),255);overlay.putpalette([250,1,251]+[0]*765);overlay.putpixel((0,0),0)
        payload=exact_gif_fixture([frames[0],overlay],disposal=1)
        with self.assertRaises(sheet_images.SheetImageError) as raised:
            self.encode(payload,[0,0])
        self.assertEqual(raised.exception.code,'GIF_EXACT_PALETTE_LIMIT')

    def test_missing_mismatched_timing_loop_and_visible_clipping_fail_closed(self):
        payload=exact_gif_fixture(exact_palette_frames())
        excessive=bytearray(payload);excessive[6:10]=b'\xff'*4
        for candidate,shifts,extra,code in [
            (b'not-gif',[0]*8,{},'GIF_SOURCE_INVALID'),
            (bytes(excessive),[0]*8,{},'GIF_SOURCE_INVALID'),
            (payload,[0]*7,{},'GIF_SOURCE_CONTRACT_MISMATCH'),
            (exact_gif_fixture(exact_palette_frames(),duration=100),[0]*8,{},'GIF_SOURCE_CONTRACT_MISMATCH'),
            (payload,[0]*8,{'loop':True},'GIF_SOURCE_CONTRACT_MISMATCH'),
            (payload,[-5]*8,{},'GIF_VISIBLE_CLIPPED')]:
            with self.subTest(code=code):
                with self.assertRaises(sheet_images.SheetImageError) as raised:self.encode(candidate,shifts,**extra)
                self.assertEqual(raised.exception.code,code)

    def test_mandatory_decode_roundtrip_detects_backend_visible_color_change(self):
        payload=exact_gif_fixture(exact_palette_frames());original=GifImagePlugin.getdata
        def changed_palette(image,**kwargs):
            image=image.copy();palette=image.getpalette();palette[0:3]=[251,1,252];image.putpalette(palette)
            return original(image,**kwargs)
        with patch.object(GifImagePlugin,'getdata',side_effect=changed_palette):
            with self.assertRaises(sheet_images.SheetImageError) as raised:self.encode(payload,[0]*8)
        self.assertEqual(raised.exception.code,'GIF_ROUNDTRIP_MISMATCH')

    def test_unsafe_transform_and_dimensions_are_rejected(self):
        payload=exact_gif_fixture(exact_palette_frames())
        for change in ({'dx':True},{'dy':1},{'scale':2},{'frame_index':True}):
            with self.subTest(change=change):
                transforms=[{'frame_index':i,'dx':0,'dy':0,'scale':1} for i in range(8)]
                transforms[0].update(change)
                with self.assertRaises(sheet_images.SheetImageError) as raised:
                    sheet_images._translated_gif_bytes(payload,transforms,input_canvas=(24,20),output_canvas=(30,20),frame_ms=150,loop=False)
                self.assertEqual(raised.exception.code,'GIF_TRANSFORM_INVALID')

    def test_neutral_palette_and_rare_late_red_never_call_quantize(self):
        frames=exact_palette_frames(colors=40)
        gray=[channel for value in range(256) for channel in (value,value,value)]
        # Keep the transparent bookkeeping entry non-identity. Pillow otherwise
        # loses the palette on an L-to-P transition in this synthetic source GIF.
        gray[-3:]=[255,0,255]
        for frame in frames:frame.putpalette(gray)
        palette=gray.copy();palette[254*3:255*3]=[145,56,59]
        frames[3].putpalette(palette);frames[3].putpixel((15,15),254)
        payload=exact_gif_fixture(frames)
        with patch.object(Image.Image,'quantize',side_effect=AssertionError('requantization forbidden')):
            result=self.encode(payload,[1]*8)
        with Image.open(io.BytesIO(result)) as child:
            child.seek(3);self.assertEqual(child.convert('RGBA').getpixel((16,15)),(145,56,59,255))
            self.assertEqual(child.convert('RGBA').getpixel((3,2)),(0,0,0,255))

    def test_full_256_color_opaque_source_with_new_transparent_margin_is_explicitly_unsupported(self):
        frame=Image.new('P',(24,20),255);frame.putpalette([c for i in range(256) for c in (i,i,(i+1)%256)])
        for i in range(256):frame.putpixel((i%24,i//24),i)
        payload=exact_gif_fixture([frame,frame.copy()],transparency=None)
        with self.assertRaises(sheet_images.SheetImageError) as raised:self.encode(payload,[0,0])
        self.assertEqual(raised.exception.code,'GIF_EXACT_PALETTE_LIMIT')

    def test_identity_gray_initial_palette_cannot_silently_lose_local_red_or_gray_remapping(self):
        # I1 reproduction is unchanged: no unused bookkeeping entry is altered.
        for local_color in ((145,56,59),(17,17,17)):
            for transparency in (None,255):
                with self.subTest(local_color=local_color,transparency=transparency):
                    frames=exact_palette_frames(colors=40)
                    gray=[c for i in range(256) for c in (i,i,i)]
                    for frame in frames:frame.putpalette(gray)
                    palette=gray.copy();palette[254*3:255*3]=list(local_color)
                    frames[3].putpalette(palette);frames[3].putpixel((15,15),254)
                    self.assertEqual(tuple(frames[3].getpalette()[254*3:255*3]),local_color)
                    source=exact_gif_fixture(frames,transparency=transparency)
                    before=bytes(source)
                    with self.assertRaises(sheet_images.SheetImageError) as raised:self.encode(source,[1]*8)
                    self.assertEqual(raised.exception.code,'GIF_SOURCE_INVALID')
                    self.assertEqual(source,before)

    def test_nonidentity_gray_global_palette_is_supported_without_conflating_gray_with_identity(self):
        for local_color in ((145,56,59),(17,17,17)):
            with self.subTest(local_color=local_color):
                frames=exact_palette_frames(colors=40)
                # All RGB channels are equal, but every index is genuinely remapped.
                gray=[c for i in range(256) for c in (255-i,255-i,255-i)]
                for frame in frames:frame.putpalette(gray)
                palette=gray.copy();palette[254*3:255*3]=list(local_color)
                frames[3].putpalette(palette);frames[3].putpixel((15,15),254)
                payload=exact_gif_fixture(frames,transparency=None)
                result=self.encode(payload,[1]*8)
                with Image.open(io.BytesIO(result)) as child:
                    child.seek(3)
                    self.assertEqual(child.convert('RGBA').getpixel((16,15)),(*local_color,255))
                    self.assertEqual(child.convert('RGBA').getpixel((3,2)),(255,255,255,255))


def minority_color_frames():
    """Many frequent earth colors, with one red pixel only in a later frame."""
    frames = []
    for i in range(8):
        frame = Image.new('RGBA', (64, 64))
        pixels = frame.load()
        for y in range(3, 61):
            for x in range(3, 61):
                pixels[x, y] = (50+(x*3+y)%150, 40+(x+y*2)%120, 30+(x*2+y)%100, 255)
        frame.putpixel((5+i, 5), (0, 0, 0, 255))
        frame.putpixel((1, 1), (255, 0, 255, 127))
        frame.putpixel((2, 1), (255, 0, 255, 128))
        frames.append(frame)
    frames[3].putpixel((32, 32), (145, 56, 59, 255))
    return frames


class GifPaletteTests(unittest.TestCase):
    def test_coverage_preserves_a_minority_red_present_only_in_frame_four(self):
        # Frequency-only quantization loses the rare red despite using all frames.
        frames = minority_color_frames()
        encoded = _gif_bytes(frames, 150, False, binary_alpha=True, palette_strategy='max_coverage')
        with Image.open(io.BytesIO(encoded)) as gif:
            gif.seek(3)
            actual = gif.convert('RGBA').getpixel((32, 32))
        self.assertLessEqual(max(abs(a-b) for a,b in zip(actual[:3], (145,56,59))), 10,
                             'A verified rare red must not become a brown/gray palette color')

    def test_historical_binary_and_legacy_bytes_stay_compatible(self):
        # Native verification depends on byte equality, not just decoded similarity.
        frames = minority_color_frames()
        self.assertEqual(hashlib.sha256(_gif_bytes(frames,150,False)).hexdigest(),
                         '7e6779b95a8ae150f1cd3bd16b065790762b492b5ad1681f841def15b5ac631f')
        self.assertEqual(hashlib.sha256(_gif_bytes(frames,150,False,binary_alpha=True)).hexdigest(),
                         'a26529bd285ba4291b21ab28e8155cabb9b0ea8d57082bcea8270e37bc75ab58')
        for binary in (False,True):
            self.assertEqual(_gif_bytes(frames,150,False,binary_alpha=binary),
                             _gif_bytes(frames,150,False,binary_alpha=binary,palette_strategy='median_cut'))

    def test_coverage_keeps_shared_palette_masks_timing_and_input_pixels(self):
        frames = minority_color_frames()
        before = [im.tobytes() for im in frames]
        for loop,threshold in ((True,128),(False,96)):
            encoded = _gif_bytes(frames,150,loop,binary_alpha=True,
                                 alpha_threshold=threshold,palette_strategy='max_coverage')
            # Parse color tables at GIF block boundaries, not incidental byte patterns.
            global_size = 3 * 2**((encoded[10]&7)+1)
            shared = encoded[13:13+global_size]
            pos = 13+global_size
            image_count = 0
            while encoded[pos] != 0x3b:
                tag = encoded[pos];pos += 1
                if tag == 0x21:
                    pos += 1  # Extension label; skip its subblocks below.
                else:
                    self.assertEqual(tag,0x2c)
                    packed = encoded[pos+8];pos += 9;image_count += 1
                    if packed & 0x80:
                        size = 3 * 2**((packed&7)+1)
                        self.assertEqual(encoded[pos:pos+size],shared)
                        pos += size
                    pos += 1  # LZW minimum code size.
                while encoded[pos]:
                    pos += 1+encoded[pos]
                pos += 1
            self.assertEqual(image_count,8)
            with Image.open(io.BytesIO(encoded)) as gif:
                self.assertEqual(gif.info['transparency'],255)
                self.assertEqual(gif.info.get('loop'),0 if loop else None)
                self.assertEqual(gif.n_frames,8)
                for i,frame in enumerate(frames):
                    gif.seek(i)
                    actual = gif.convert('RGBA')
                    expected = frame.getchannel('A').point(lambda a:255 if a>=threshold else 0)
                    self.assertEqual(actual.getchannel('A').tobytes(),expected.tobytes())
                    self.assertEqual(actual.getpixel((5+i,5)),(0,0,0,255))
                    self.assertEqual(gif.info['duration'],150)
        self.assertEqual([im.tobytes() for im in frames],before)

    def test_palette_strategy_rejects_unknown_values_and_legacy_coverage(self):
        frames = minority_color_frames()
        for invalid in (None,'',True,0,[],{},'MAXCOVERAGE','median'):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                _gif_bytes(frames,150,False,binary_alpha=True,palette_strategy=invalid)
        with self.assertRaises(ValueError):
            _gif_bytes(frames,150,False,binary_alpha=False,palette_strategy='max_coverage')


class GifTransparencyTests(unittest.TestCase):
    def test_explicit_threshold_keeps_a_thin_link_without_faint_halo(self):
        frames = []
        for i in range(8):
            frame = Image.new('RGBA', (24, 24))
            draw = ImageDraw.Draw(frame)
            draw.rectangle((8, 10, 16, 21), fill=(40+i*20, 70, 90, 255))
            draw.rectangle((11, 2, 13, 5), fill=(50, 40, 30, 255))
            draw.line((12, 6, 12, 9), fill=(50, 40, 30, 126))
            frame.putpixel((3, 3), (255, 255, 255, 15))
            frames.append(frame)
        before = [frame.tobytes() for frame in frames]
        default = _gif_bytes(frames, 150, False, binary_alpha=True)
        explicit_default = _gif_bytes(frames, 150, False, binary_alpha=True, alpha_threshold=128)
        self.assertEqual(default, explicit_default)
        repaired = _gif_bytes(frames, 150, False, binary_alpha=True, alpha_threshold=96)
        with Image.open(io.BytesIO(default)) as old, Image.open(io.BytesIO(repaired)) as new:
            self.assertEqual(new.n_frames, 8)
            self.assertNotIn('loop', new.info)
            for i in range(8):
                old.seek(i);new.seek(i)
                old_rgba=old.convert('RGBA');new_rgba=new.convert('RGBA')
                self.assertEqual(old_rgba.getpixel((12,7))[3],0)
                self.assertEqual(new.info['duration'],150)
                for y in range(2,22):
                    self.assertEqual(new_rgba.getpixel((12,y))[3],255)
                self.assertEqual(new_rgba.getpixel((3,3))[3],0)
        self.assertEqual(before,[frame.tobytes() for frame in frames])
        for invalid in (0,256,True,96.5):
            with self.assertRaises(ValueError):
                _gif_bytes(frames,150,False,binary_alpha=True,alpha_threshold=invalid)

    def test_v3_gif_preserves_binary_opacity_without_promoting_faint_pixels(self):
        # Removing the alpha threshold must make this fail: RGBA -> GIF alone
        # promotes the near-transparent white fringe to an opaque outline.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sheet = Image.new('RGBA', (128, 64))
            for i in range(8):
                x, y = (i % 4) * 32, (i // 4) * 32
                draw = ImageDraw.Draw(sheet)
                draw.rectangle((x+9, y+7, x+21, y+24), fill=(40+i*20, 70, 90, 255))
                draw.rectangle((x+12, y+10, x+14, y+12), fill=(255, 0, 255, 255))
                for dx, alpha in enumerate((0, 1, 15, 127, 128, 254, 255)):
                    sheet.putpixel((x+8+dx, y+5), (255, 255, 255, alpha))
            raw = root/'input.png'
            sheet.save(raw)
            for loop, frame_ms in ((True, 100), (False, 150)):
                with self.subTest(loop=loop):
                    result = assemble_sheet(raw, root/str(loop), frame_count=8,
                        columns=4, rows=2, canvas=(48,48), frame_ms=frame_ms, loop=loop)
                    with Image.open(result['artifact_paths']['preview_gif']) as gif:
                        self.assertEqual(gif.n_frames, 8)
                        self.assertEqual(gif.info.get('loop'), 0 if loop else None)
                        for index, path in enumerate(result['artifact_paths']['frames']):
                            gif.seek(index)
                            self.assertEqual(gif.info['duration'], frame_ms)
                            decoded = gif.convert('RGBA')
                            with Image.open(path) as frame:
                                expected = frame.getchannel('A').point(lambda a: 255 if a >= 128 else 0)
                                self.assertEqual(decoded.getchannel('A').tobytes(), expected.tobytes(),
                                    f'frame {index+1}: GIF must not make faint PNG pixels opaque')
                                # A color used for transparency bookkeeping must
                                # never erase an equally colored opaque feature.
                                for xy in ((x,y) for y in range(frame.height) for x in range(frame.width)):
                                    if frame.getpixel(xy) == (255,0,255,255):
                                        self.assertEqual(decoded.getpixel(xy)[3],255)


if __name__ == '__main__':
    unittest.main()
